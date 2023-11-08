# Copyright Amazon Web Services and its Affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import pickle
import os
import itertools
import torch
from transformers_neuronx import base
from transformers_neuronx import bucket
from transformers_neuronx import compiler
from transformers_neuronx import dtypes
from transformers_neuronx import hlo
from transformers_neuronx import ops
from transformers_neuronx import parallel
from transformers_neuronx import utils
from transformers_neuronx import quantize
from transformers_neuronx import constants
from transformers_neuronx.config import NeuronConfig
from transformers_neuronx.utils import interleave_qkv

from concurrent.futures import ProcessPoolExecutor


class DecoderLmHeadForSamplingNoEmbedding(torch.nn.Module, base.NeuronBaseSerializer):

    def __init__(self, tp_degree, n_positions_list, n_active_tokens, batch_size,
                 attention_head_size, amp, num_layers, n_head=None, n_kv_head=0,
                 unroll=None, neuron_config=None, allow_pad=True, prefixed_length=0,
                 shard_over_batch=False, n_parallel_output_tokens=1, builder=None):
        super().__init__()
        if unroll is None:
            unroll = num_layers
        self.tp_degree = tp_degree
        self.n_positions_list = n_positions_list
        self.n_active_tokens = n_active_tokens
        self.batch_size = list()
        if isinstance(batch_size,int):
            self.batch_size = [batch_size]
        elif isinstance(batch_size,list):
            self.batch_size = sorted(batch_size)
        else:
            raise TypeError("batch_size must be list of ints or int type")
        if neuron_config and neuron_config.continuous_batching:
            self.batch_size_for_shared_caches = neuron_config.continuous_batching.batch_size_for_shared_caches
            # Use batch size 1 for parallel context encoding in continuous batching
            if n_active_tokens > 1:
                assert len(self.batch_size) == 1 and self.batch_size[-1] == 1, "invalid batch_size for continuous batching"
        self.attention_head_size = attention_head_size  # TODO: rename to size_per_head
        self.n_head = n_head
        self.n_kv_head = n_kv_head if (n_kv_head > 0) else n_head
        self.shard_over_batch = shard_over_batch
        self.n_parallel_output_tokens=n_parallel_output_tokens
        self.amp = amp
        self.num_layers = num_layers
        self.unroll = unroll
        self.neuron_config = NeuronConfig() if neuron_config is None else neuron_config
        self.prefixed_length = prefixed_length
        self.layers = torch.nn.ModuleList()
        self.ln_f_weight = None
        self.ln_f_bias = None
        self.lm_head_weight = None
        self.lm_head_bias = None
        self.inputs_sdim = None
        self.inputs_builder = None
        self.layer_builder = None
        self.ln_lm_head_builder = None
        self.program = None
        self.pre_layer_parameters = []
        self.pre_layer_builder = None
        self.allow_pad = allow_pad
        self.use_executor = False
        self.return_ranks = -1
        self.need_reorder_cache = False
        self.compiler_artifacts_path = None
        self.hlo_builder=builder

    def init_context_decoder(self, unroll, buckets, model_obj):
        decoder_lm_head = {}
        self.context_batch_sizes = [1] if self.neuron_config and self.neuron_config.continuous_batching else self.batch_sizes
        for context_length_estimate in buckets:
            for batch_size in self.context_batch_sizes:
                decoder_lm_head[context_length_estimate, batch_size] = DecoderLmHeadForSamplingNoEmbedding(
                    tp_degree=self.tp_degree, 
                    n_positions_list=[context_length_estimate], 
                    n_active_tokens=context_length_estimate, 
                    batch_size=batch_size, 
                    attention_head_size=self.attention_head_size, 
                    amp=self.amp,
                    num_layers=self.num_layers,
                    n_head=self.n_head,
                    n_kv_head=self.n_kv_head,
                    unroll=unroll,
                    neuron_config=self.neuron_config, 
                    allow_pad=self.allow_pad
                )
                base.NeuronModelBase.register_for_serialization(model_obj,decoder_lm_head[context_length_estimate, batch_size])
        return decoder_lm_head
        

    def init_token_decoder(self,unroll, buckets, model_obj):
        decoder_lm_head = DecoderLmHeadForSamplingNoEmbedding(
            tp_degree=self.tp_degree, 
            n_positions_list=buckets, 
            n_active_tokens=1, 
            batch_size=self.batch_size, 
            attention_head_size=self.attention_head_size, 
            amp=self.amp,
            num_layers=self.num_layers,
            n_head=self.n_head,
            n_kv_head=self.n_kv_head,
            unroll=unroll,
            neuron_config=self.neuron_config, 
            allow_pad=True, 
            shard_over_batch=self.shard_over_batch
        )
        base.NeuronModelBase.register_for_serialization(model_obj,decoder_lm_head)
        decoder_lm_head.add_inputs_builder(self.hlo_builder.inputs)
        decoder_lm_head.add_layer_builder(self.hlo_builder.layer)
        decoder_lm_head.add_ln_lm_head_builder(self.hlo_builder.ln_lm_head)
        return decoder_lm_head
    
    def init_speculative_decoder(self, unroll, buckets, model_obj, n_active_tokens):
        decoder_lm_head = DecoderLmHeadForSamplingNoEmbedding(
            tp_degree=self.tp_degree, 
            n_positions_list=buckets, 
            n_active_tokens=n_active_tokens, 
            batch_size=self.batch_size, 
            attention_head_size=self.attention_head_size, 
            amp=self.amp,
            num_layers=self.num_layers,
            n_head=self.n_head,
            n_kv_head=self.n_kv_head,
            unroll=unroll,
            neuron_config=self.neuron_config, 
            allow_pad=True, 
            shard_over_batch=self.shard_over_batch,
            n_parallel_output_tokens= n_active_tokens
        )
        base.NeuronModelBase.register_for_serialization(model_obj,decoder_lm_head)
        return decoder_lm_head

    def setup_reorder_cache(self):
        self.need_reorder_cache = True

    def enable_executor(self, return_ranks=-1):
        self.use_executor = True
        self.return_ranks = return_ranks
        self.program.enable_executor()

    def add_inputs_builder(self, inputs_builder):
        self.inputs_builder = inputs_builder

    def add_pre_layer_parameter(self, param, sharding=None, allow_pad=False):
        self.pre_layer_parameters.append((param, sharding, allow_pad))

    def add_pre_layer_builder(self, builder):
        self.pre_layer_builder = builder

    def add_layer_builder(self, layer_builder):
        self.layer_builder = layer_builder

    def add_ln_lm_head_builder(self, ln_lm_head_builder):
        self.ln_lm_head_builder = ln_lm_head_builder

    def new_layer(self):
        *_, n_positions = self.n_positions_list
        layer = DecoderLayer(self.tp_degree, n_positions, self.batch_size, self.attention_head_size,
                             amp=self.amp, neuron_config=self.neuron_config, allow_pad=self.allow_pad, n_active_tokens=self.n_active_tokens,
                             n_head=self.n_head, n_kv_head=self.n_kv_head, shard_over_batch=self.shard_over_batch)
        self.layers.append(layer)
        return layer

    def add_final_layer_norm(self, weight, bias):
        self.ln_f_weight = weight
        self.ln_f_bias = bias

    def add_lm_head(self, weight, bias=None):
        self.lm_head_weight = weight
        self.lm_head_bias = bias

    def to_neuron(self):
        manipulator = MaybeParallelTensorManipulator(self.tp_degree)

        extras = []
        for param, dim, allow_pad in self.pre_layer_parameters:
            if allow_pad:
                if param.shape[dim] % self.tp_degree != 0:
                    size = utils.round_up_to_divisor(param.shape[dim], self.tp_degree)
                    param = utils.pad(param, dim, size)
            extras.append(manipulator.duplicate_or_shard_along(param, dim))
        self.pre_layer_parameters = extras

        self.ln_f_weight = manipulator.duplicate(self.ln_f_weight)
        self.ln_f_bias = manipulator.duplicate(self.ln_f_bias)
        _, vocab_size = self.lm_head_weight.shape
        # Pad vocab size such that it can be divided by the following factor
        divisor = int(os.environ.get('NEURON_VOCAB_PAD_DIVISOR', str(self.tp_degree)))
        vocab_pad = utils.pad_vocab_size(vocab_size, divisor)
        lm_head_weight = torch.nn.functional.pad(self.lm_head_weight, (0, vocab_pad, 0, 0))
        self.lm_head_weight = manipulator.shard_along(lm_head_weight, dim=1)
        ln_lm_head_params = [*self.pre_layer_parameters, self.ln_f_weight, self.ln_f_bias, self.lm_head_weight]
        ln_lm_head_params = [param for param in ln_lm_head_params if param is not None]
        if self.lm_head_bias is not None:
            self.lm_head_bias = manipulator.shard_along(self.lm_head_bias, dim=0)
            ln_lm_head_params.append(self.lm_head_bias)

        self.program = self._build_program()
        # setup_reorder_cache needs to be able to be called before to_neuron()
        # and after to_neuron for backwards compatability.
        # If called before to_neuron() this logic will be reached and it will
        # create the HLO for reorder_cache, check if there is an available NEFF
        # for deserialization, then compile. If called after, setup_reorder_cache
        # will be called from the NeuronModelBase without serialization logic.
        if self.need_reorder_cache:
            self.program.setup_reorder_cache(also_compile_now=False)
        if self.compiler_artifacts_path is not None:
            self.set_neff_bytes()
        self.program.setup(self.layers, ln_lm_head_params)
        # separate intialization and compilation
        if self.need_reorder_cache:
            self.program.setup_reorder_cache_kernels()


    def build_weight_shared(self, n_positions_list=None, n_active_tokens=None, batch_size=None,
                            unroll=None, share_caches=False, new=None):
        if new == None:
            new = DecoderLmHeadForSamplingNoEmbedding(
                self.tp_degree, self.n_positions_list, self.n_active_tokens, self.batch_size, self.attention_head_size,
                amp=self.amp, num_layers=self.num_layers, n_head=self.n_head, n_kv_head=self.n_kv_head,
                unroll=self.unroll, neuron_config=self.neuron_config, allow_pad=self.allow_pad,
                prefixed_length=self.prefixed_length, n_parallel_output_tokens=self.n_parallel_output_tokens
            )
        new.add_inputs_builder(self.inputs_builder)
        new.add_pre_layer_builder(self.pre_layer_builder)
        new.add_layer_builder(self.layer_builder)
        new.add_ln_lm_head_builder(self.ln_lm_head_builder)
        for layer in self.layers:
            new_layer = new.new_layer()
            new_layer.assign_parameters(layer)
            if share_caches:
                buckets_from_src = self.neuron_config and self.neuron_config.continuous_batching
                new_layer.assign_caches(layer, buckets_from_src=buckets_from_src)
            else:
                new_layer.init_caches()
            new_layer.extra_parameters = layer.extra_parameters
        new.pre_layer_parameters = self.pre_layer_parameters
        new.add_final_layer_norm(self.ln_f_weight, self.ln_f_bias)
        new.add_lm_head(self.lm_head_weight, self.lm_head_bias)
        ln_lm_head_params = [*new.pre_layer_parameters, new.ln_f_weight, new.ln_f_bias, new.lm_head_weight]
        ln_lm_head_params = [param for param in ln_lm_head_params if param is not None]
        if new.lm_head_bias is not None:
            ln_lm_head_params.append(new.lm_head_bias)
        new.program = new._build_program()
        if new.compiler_artifacts_path is not None:
            new.set_neff_bytes()
        new.program.setup(new.layers, ln_lm_head_params)
        return new

    def reset(self):
        for layer in self.layers:
            layer.reset()

    def forward_single(self, *inputs):
        """
        Fast-path forward function which avoids as much overhead as possible.

        This path makes the assumption that inputs are correctly sized for a
        sequence length of 1. This allows us to avoid checking buckets, slicing,
        etc.
        """
        hidden, cache_ids, *_ = inputs
        batch_size = hidden.shape[2]
        # In continuous batching, take largest cache_id and use the power-of-two policy to find the appropriate bucket.
        if self.neuron_config and self.neuron_config.continuous_batching:
            bucket_id = 0
        else:
            bucket_id = self.program.find_bucket_id(cache_ids.item())
        if self.use_executor:
            return self.program.execute(bucket_id, batch_size, *inputs, return_ranks=self.return_ranks)
        else:
            self.program.inputs_host_to_device(inputs, batch_size)
            self.program.run(bucket_id, batch_size)
            return self.program.logits_device_to_host(batch_size)

    def forward(self, *inputs):
        hidden, *_ = inputs
        # batch size is in dim 2 because hidden is now transposed in model.py's forward function
        batch_size = hidden.shape[2]
        sequence_dim, *_ = self.inputs_sdim
        sequence_length = hidden.shape[sequence_dim]
        if sequence_length == 1:
            return self.forward_single(*inputs)
        
        outputs = None
        slice_loop_var = range(0, sequence_length, self.n_active_tokens)
        if self.n_parallel_output_tokens > 1:
            slice_loop_var = [0]
  
        for start in slice_loop_var:
            slicing = slice(start, start + self.n_active_tokens)
            input_tensors = []
            for sdim, tensor in zip(self.inputs_sdim, inputs):
                if sdim is not None:
                    slices = [slice(None) for _ in tensor.shape]
                    slices[sdim] = slicing
                    tensor = tensor[tuple(slices)].contiguous()
                input_tensors.append(tensor)
            _, cache_ids, *_ = input_tensors
            max_id = cache_ids.max().item()
            min_id = cache_ids.min().item()
            # When context_length == m * n_active_tokens, bucket-size of n_active_tokens should be chosen.
            # This is useful for Fusion-In-Decoder case, where 2nd n_active_tokens don't need to attend to
            # 1st n_active_tokens.
            bucket_id = self.program.find_bucket_id(max_id)
            if self.use_executor:
                outputs = self.program.execute(bucket_id, batch_size, *input_tensors, return_ranks=self.return_ranks)
            else:
                self.program.inputs_host_to_device(input_tensors, batch_size)
                self.program.run(bucket_id, batch_size)

        if not self.use_executor:
            outputs = self.program.logits_device_to_host(batch_size)
        return outputs

    def embed_positions_ids(self, position_ids, start_ids=None, batch_size=None):
        if batch_size is None:
            assert len(self.batch_size) == 1,"batch_size should be specified if model compiled with multiple batch sizes"
            batch_size = self.batch_size[0]
        if start_ids is None:
            return position_ids, torch.zeros([batch_size], dtype=torch.int32)
        position_ids = position_ids.unsqueeze(0).repeat(batch_size, 1)
        position_ids -= start_ids.unsqueeze(1)
        position_ids.masked_fill_(position_ids < 0, 0)
        return position_ids, start_ids

    def _build_program(self):
        hlo_modules = dict()
        if self.unroll == self.num_layers:
            for npos,batch_size in itertools.product(self.n_positions_list, self.batch_size):
                hlo_modules[npos,batch_size] = self._hlo_fully_unrolled(npos, batch_size)
            num_inputs = len(self.inputs_sdim)
            program = DecoderProgramFullyUnrolled(self.layers, hlo_modules, num_inputs, self.tp_degree, self.n_positions_list, self.batch_size, self.prefixed_length, batch_size_for_shared_caches=self.batch_size_for_shared_caches)
        else:
            if utils.amp_is_u8(self.amp):
                raise NotImplementedError(f'amp={self.amp} only supports fully unrolled decoder')
            for npos,batch_size in itertools.product(self.n_positions_list, self.batch_size):
                hlo_modules[npos,batch_size] = self._hlo_multi_layer(npos,batch_size)

            ln_lm_head_hlo_modules = [self._hlo_ln_lm_head(batch_size) for batch_size in self.batch_size]
            num_inputs = len(self.inputs_sdim)
            program = DecoderProgramMultiLayer(self.layers, hlo_modules, ln_lm_head_hlo_modules, num_inputs,
                                                self.num_layers, self.unroll, self.tp_degree,
                                                self.n_positions_list, self.batch_size, self.prefixed_length)

        return program

    def _hlo_fully_unrolled(self, n_positions, batch_size):

        def fully_unrolled(scribe):
            amp, quantized, dequantized = utils.parse_amp(self.amp)
            dtype = getattr(scribe, amp)
            (hidden, *tensors), self.inputs_sdim = self.inputs_builder(
                scribe, dtype, n_positions, self.n_active_tokens, batch_size)
            last_token_id = tensors[0]
            param_builder = DecoderParameterBuilder(scribe, len(self.inputs_sdim))
            layers_caches, layers_weights = self._hlo_layers_params(param_builder, self.layers, n_positions, batch_size)
            hidden = maybe_transfer_with_static_ring(hidden)
            hidden, tensors = self._hlo_pre_layer(hidden, tensors, param_builder)
            ln_f_weight = param_builder.from_tensor(self.ln_f_weight)
            ln_f_bias = param_builder.from_tensor(self.ln_f_bias)
            head_weight = param_builder.from_tensor(self.lm_head_weight)
            head_bias = param_builder.from_tensor(self.lm_head_bias)
            hidden, out_caches = self._hlo_layers(hidden, tensors, self.layers, layers_caches, layers_weights)
            ln_f_weight = maybe_transfer_with_static_ring(ln_f_weight)
            ln_f_bias = maybe_transfer_with_static_ring(ln_f_bias)
            head_weight = maybe_transfer_with_static_ring(head_weight)
            head_bias = maybe_transfer_with_static_ring(head_bias)
            logits = self.ln_lm_head_builder(hidden, last_token_id, ln_f_weight, ln_f_bias, head_weight, head_bias, self.n_parallel_output_tokens)
            outputs = [logits, *out_caches]
            root_shapes = [shape.dtype[shape.sizes] for shape in outputs]
            return scribe.tuple(*root_shapes).Tuple(*outputs)

        return compiler.compile_py_func(fully_unrolled)

    def _hlo_multi_layer(self, n_positions, batch_size):

        def multi_layer(scribe):
            dtype = getattr(scribe, self.amp)
            (hidden, *tensors), self.inputs_sdim = self.inputs_builder(
                scribe, dtype, n_positions, self.n_active_tokens, batch_size)
            param_builder = DecoderParameterBuilder(scribe, len(self.inputs_sdim))
            # use the first `unroll` layers to build the HLO -- assuming all layers are same
            layers = self.layers[:self.unroll]
            layers_caches, layers_weights = self._hlo_layers_params(param_builder, layers, n_positions, batch_size)
            hidden, tensors = self._hlo_pre_layer(hidden, tensors, param_builder)
            out_hidden, out_caches = self._hlo_layers(hidden, tensors, layers, layers_caches, layers_weights)
            out_hidden.set_alias_to(hidden)
            outputs = [out_hidden, *out_caches]
            root_shapes = [shape.dtype[shape.sizes] for shape in outputs]
            return scribe.tuple(*root_shapes).Tuple(*outputs)

        return compiler.compile_py_func(multi_layer)

    def _hlo_pre_layer(self, hidden, tensors, param_builder):
        params = []
        if self.pre_layer_builder is not None:
            for param in self.pre_layer_parameters:
                param = param_builder.from_tensor(param)
                param = hlo.transfer_with_static_ring(param)
                params.append(param)
            (hidden, *tensors) = self.pre_layer_builder(hidden, *tensors, *params)
        return hidden, tensors

    def _hlo_layers_params(self, param_builder, layers, n_positions, batch_size):
        layers_caches = []
        if self.batch_size_for_shared_caches:
            batch_size = self.batch_size_for_shared_caches
        for layer in layers:
            layer_caches = []
            for cache in layer.attn_k_cache[batch_size], layer.attn_v_cache[batch_size]:
                par = param_builder.from_tensor(cache, dim_size={0: n_positions})
                layer_caches.append(par)
            layers_caches.append(layer_caches)
        layers_weights = []
        for layer in layers:
            layer_weights = [param_builder.from_tensor(weight) for weight in layer.all_parameters()]
            layers_weights.append(layer_weights)
        return layers_caches, layers_weights

    def _hlo_layers(self, hidden, tensors, layers, layers_caches, layers_weights):
        output_caches = []
        for layer, caches, weights in zip(layers, layers_caches, layers_weights):
            in_caches = [hlo.transfer_with_static_ring(cache) for cache in caches]
            weights = [maybe_transfer_with_static_ring(weight) for weight in weights]
            weights = layer.hlo_maybe_dequantize_weights(weights)
            hidden, *out_caches = self.layer_builder(hidden, *tensors, *in_caches, *weights)
            for out_cache, cache in zip(out_caches, caches):
                out_cache.set_alias_to(cache, must=True)
            output_caches.extend(out_caches)
        return hidden, output_caches

    def _hlo_ln_lm_head(self, batch_size):
        hidden_sizes = []

        def capture_hidden_sizes(scribe):
            dtype = getattr(scribe, self.amp)
            *_, n_positions = self.n_positions_list
            (hidden, *_), _ = self.inputs_builder(
                scribe, dtype, n_positions, self.n_active_tokens, batch_size)
            hidden_sizes.clear()
            hidden_sizes.extend(hidden.sizes)
            return hidden

        compiler.compile_py_func(capture_hidden_sizes)

        def ln_lm_head(scribe):
            dtype = getattr(scribe, self.amp)
            hidden = dtype[tuple(hidden_sizes)].Parameter(parameter_number=0)
            next_tok_id = scribe.s32.Parameter(parameter_number=1)
            param_builder = DecoderParameterBuilder(scribe, 2)
            ln_f_weight = param_builder.from_tensor(self.ln_f_weight)
            ln_f_bias = param_builder.from_tensor(self.ln_f_bias)
            head_weight = param_builder.from_tensor(self.lm_head_weight)
            head_bias = param_builder.from_tensor(self.lm_head_bias)
            return self.ln_lm_head_builder(hidden, next_tok_id, ln_f_weight, ln_f_bias, head_weight, head_bias, self.n_parallel_output_tokens)

        return compiler.compile_py_func(ln_lm_head)

    # Mainly used for serialization purposes.
    # Defines how to access all the kernels.
    def get_all_kernels(self):
        return self.program.get_kernels()

def read_n_position(hlo_module, num_inputs):
    return hlo_module.host_program_shape.parameters[num_inputs].dimensions[0]


def read_n_active_tokens(hlo_module):
    return hlo_module.host_program_shape.parameters[0].dimensions[1]


def read_batch_size(hlo_module):
    return hlo_module.host_program_shape.parameters[0].dimensions[0]


def maybe_transfer_with_static_ring(shape):
    if shape is None:
        return None
    return hlo.transfer_with_static_ring(shape)

### This is a place-holder to indicate what we want this to look like
### This is not currently utilized anywhere
### TO-DO: Modify/integrate these to have decoder-specific forward functionality
class SpeculativeDecoder(torch.nn.Module):
    def forward(self, hidden, *args):
        hidden = hidden.transpose(0, -1).contiguous()
        logits = self.decoder_lm_head(hidden, *args)
        logits = logits.to(torch.float32)
        logits = logits[:self.config.vocab_size, -self.n_parallel_output_tokens:, :]
        logits = logits.transpose(0, 1)
        logits=logits.transpose(1, 2)
        return logits

### This is a place-holder to indicate what we want this to look like
### This is not currently utilized anywhere
### TO-DO: Modify/integrate these to have decoder-specific forward functionality
class ContextDecoder(torch.nn.Module):

    def context(self, hidden, cache_ids, start_ids, last_token_id):
        """A helper to process context (prompt)
        1) if there is available context encoding model (infered from self.context_buckets)
            - when context_length >= estimate, slice the context up to estimate,
                and call context encoding model
            - when context_length < estimate, skip and fall back to serial token generation model

            and mark `current` accordingly

        2) process the left over tokens accroding to `current`
            - if there is no context encoding model, simply do serial token generation for context
        """
        context_length = hidden.shape[1]
        # batch_size is in dim 2 because of the transpose taken in _forward function
        batch_size = hidden.shape[2]

        if self.is_fid:
            # Fusion-In-Decoder context encoding
            fused_context_length = hidden.shape[1]
            context_length = fused_context_length // self.batch_size

        current = 0

        estimate = bucket.find(self.context_buckets, context_length)


        if estimate is not None:
            hidden_context = hidden
            cache_context = cache_ids

            # Slice context that when it is too large
            if context_length > estimate:
                current = estimate
                hidden_context = hidden[:, :estimate]
                cache_context = cache_ids[:estimate]

            # Cannot use context encoding for a context that is too small. This
            # is because the caller must be aware of the cache-ids/start-ids
            # used.
            elif context_length < estimate:
                raise ValueError(f"context_length ({context_length}) shouldn't be smaller than estimate ({estimate})")

            # Directly pass input to the context network when exactly sized
            else:
                current = estimate

            if current == estimate:
                model = self.decoder_lm_head_for_context[estimate, batch_size]
                logits = model(hidden_context, cache_context, start_ids, last_token_id)

        for i in range(current, context_length):
            cache_ids = torch.as_tensor([i], dtype=torch.int32)
            hidden_slice = hidden[:, i:i+1].contiguous()
            logits = self.decoder_lm_head(hidden_slice, cache_ids, start_ids, last_token_id)

        if self.is_fid:
            logits[:] = float('-inf')
            logits[self.bos_token_id] = 1.0

        return logits

    def forward(self, hidden, cache_ids=None, start_ids=None, last_token_id=None):
        hidden = hidden.transpose(0, -1).contiguous()
        logits = self.context(hidden, cache_ids, start_ids, last_token_id)
        logits = logits.to(torch.float32)
        logits = logits[:self.config.vocab_size, -1, :] 
        logits = logits.transpose(0, 1)
        return logits

### This is a place-holder to indicate what we want this to look like
### This is not currently utilized anywhere
### TO-DO: Modify/integrate these to have decoder-specific forward functionality
class TokenDecoder(torch.nn.Module):
    def forward(self, hidden, *args):
        hidden = hidden.transpose(0, -1).contiguous()
        logits = TokenDecoder.forward(hidden, *args)
        logits = logits.to(torch.float32)
        logits = logits[:self.config.vocab_size, -1, :] 
        logits = logits.transpose(0, 1)
        return logits



class MaybePadder:

    def __init__(self, size) -> None:
        self.size = size

    def __call__(self, weight, dim):
        return utils.pad(weight, dim, self.size)


class DecoderLayer(torch.nn.Module):

    def __init__(self, tp_degree, n_positions, batch_size, attention_head_size, n_head, amp,
                 n_kv_head=0, neuron_config=None, allow_pad=False, n_active_tokens=None, shard_over_batch=False):
        super().__init__()
        self.pre_attn_ln_weight = None
        self.pre_attn_ln_bias = None
        self.attn_q_weight = None
        self.attn_q_scales = None
        self.attn_q_bias = None
        self.attn_k_weight = None
        self.attn_k_scales = None
        self.attn_k_bias = None
        self.attn_v_weight = None
        self.attn_v_scales = None
        self.attn_v_bias = None
        self.attn_out_weight = None
        self.attn_out_scales = None
        self.attn_out_bias = None
        self.post_attn_ln_weight = None
        self.post_attn_ln_bias = None
        self.pre_mlp_ln_weight = None
        self.pre_mlp_ln_bias = None
        self.mlp_in_weight = None
        self.mlp_in_scales = None
        self.mlp_in_bias = None
        self.mlp_out_weight = None
        self.mlp_out_scales = None
        self.mlp_out_bias = None
        self.post_mlp_ln_weight = None
        self.post_mlp_ln_bias = None
        self.attn_q_min = None
        self.attn_q_max = None
        self.attn_k_min = None
        self.attn_k_max = None
        self.attn_v_min = None
        self.attn_v_max = None
        self.attn_out_min = None
        self.attn_out_max = None
        self.mlp_in_min = None
        self.mlp_in_max = None
        self.mlp_out_min = None
        self.mlp_out_max = None
        # Create KV caches for each batch_size
        self.attn_k_cache = dict()
        self.attn_v_cache = dict()
        self.cache_shape = dict()
        self.tp_degree = tp_degree
        self.n_positions = n_positions
        self.n_head = n_head
        self.n_kv_head = n_kv_head
        self.batch_sizes = batch_size
        self.attention_head_size = attention_head_size  # TODO: rename this to size_per_head
        self.tp_degree = tp_degree
        self.amp = amp
        dtype, _, _ = utils.parse_amp(amp)
        self.cache_dtype = dtypes.to_torch_dtype(dtype)
        self.neuron_config = NeuronConfig() if neuron_config is None else neuron_config
        self.extra_parameters = []
        self.allow_pad = allow_pad
        self.shard_over_batch = shard_over_batch
        self.attn_out_sharding = 0
        self.attn_out_transposed = True
        self.mlp_out_sharding = 0
        self.mlp_out_transposed = True

    def add_parameter(self, param, sharding=None, allow_pad=False, allow_quantize=False,
                      out_feature_dim=1, allow_transform=False):
        self.extra_parameters.append((param, sharding, allow_pad, allow_quantize, out_feature_dim, allow_transform))

    def add_pre_attention_layer_norm(self, weight, bias):
        self.pre_attn_ln_weight = weight
        self.pre_attn_ln_bias = bias

    def add_attention_query(self, weight, bias):
        self.attn_q_weight = weight
        self.attn_q_bias = bias

    def add_attention_key(self, weight, bias):
        self.attn_k_weight = weight
        self.attn_k_bias = bias

    def add_attention_value(self, weight, bias):
        self.attn_v_weight = weight
        self.attn_v_bias = bias

    def add_attention_output(self, weight, bias, sharding=0, transposed=True):
        self.attn_out_weight = weight
        self.attn_out_bias = bias
        self.attn_out_sharding = sharding
        self.attn_out_transposed = transposed

    def add_post_attention_layer_norm(self, weight, bias):
        self.post_attn_ln_weight = weight
        self.post_attn_ln_bias = bias

    def add_pre_mlp_layer_norm(self, weight, bias):
        self.pre_mlp_ln_weight = weight
        self.pre_mlp_ln_bias = bias

    def add_mlp_input(self, weight, bias):
        self.mlp_in_weight = weight
        self.mlp_in_bias = bias

    def add_mlp_output(self, weight, bias, sharding=0, transposed=True):
        self.mlp_out_weight = weight
        self.mlp_out_bias = bias
        self.mlp_out_sharding = sharding
        self.mlp_out_transposed = transposed

    def add_post_mlp_layer_norm(self, weight, bias):
        self.post_mlp_ln_weight = weight
        self.post_mlp_ln_bias = bias

    def to_neuron(self):

        # If we allow padding then we need to pad non-sharded QKV weight dimensions
        if self.allow_pad:
            # Hidden size padding
            hidden_size, _ = self.attn_q_weight.shape
            n_heads = hidden_size // self.attention_head_size
            n_heads_padded = utils.round_up_to_divisor(n_heads, self.tp_degree)
            hidden_size_padded = n_heads_padded * self.attention_head_size
            maybe_pad = MaybePadder(hidden_size_padded)

            self.attn_q_weight = maybe_pad(self.attn_q_weight, dim=1)
            self.attn_q_bias = maybe_pad(self.attn_q_bias, dim=0)

            if self.n_head == self.n_kv_head:
                self.attn_k_weight = maybe_pad(self.attn_k_weight, dim=1)
                self.attn_k_bias = maybe_pad(self.attn_k_bias, dim=0)

                self.attn_v_weight = maybe_pad(self.attn_v_weight, dim=1)
                self.attn_v_bias = maybe_pad(self.attn_v_bias, dim=0)

            if self.neuron_config and self.neuron_config.fuse_qkv:
                fused_qkv_weight = interleave_qkv(self.attn_q_weight, self.attn_k_weight, self.attn_v_weight, self.tp_degree, dim=1)
                if self.attn_q_bias is not None:
                    fused_qkv_bias = interleave_qkv(self.attn_q_bias, self.attn_k_bias, self.attn_v_bias, self.tp_degree, dim=0)
                else:
                    fused_qkv_bias = None
                fused_qkv_scales = None
                self.attn_k_weight = None
                self.attn_k_scales = None
                self.attn_k_bias = None
                self.attn_v_weight = None
                self.attn_v_scales = None
                self.attn_v_bias = None
            self.attn_out_weight = maybe_pad(self.attn_out_weight, dim=self.attn_out_sharding)

            # Intermediate MLP layer padding
            if self.mlp_in_weight is not None:
                _, intermediate_size = self.mlp_in_weight.shape
                intermediate_size_padded = utils.round_up_to_divisor(intermediate_size, self.tp_degree)
                if os.environ.get("NEURON_INTERNAL_TRANSFORM_WEIGHT_LAYOUT", None):
                    intermediate_size_padded = \
                        utils.round_up_to_divisor(intermediate_size // self.tp_degree,
                                                  constants.TILE_SIZE) * self.tp_degree
                maybe_pad = MaybePadder(intermediate_size_padded)

                self.mlp_in_weight = maybe_pad(self.mlp_in_weight, dim=1)
                self.mlp_in_bias = maybe_pad(self.mlp_in_bias, dim=0)
                self.mlp_out_weight = maybe_pad(self.mlp_out_weight, dim=self.mlp_out_sharding)

        if utils.amp_is_u8(self.amp):
            self.attn_q_weight, self.attn_q_min, self.attn_q_max = utils.u8_encode(self.attn_q_weight)
            self.attn_k_weight, self.attn_k_min, self.attn_k_max = utils.u8_encode(self.attn_k_weight)
            self.attn_v_weight, self.attn_v_min, self.attn_v_max = utils.u8_encode(self.attn_v_weight)
            self.attn_out_weight, self.attn_out_min, self.attn_out_max = utils.u8_encode(self.attn_out_weight)
            self.mlp_in_weight, self.mlp_in_min, self.mlp_in_max = utils.u8_encode(self.mlp_in_weight)
            self.mlp_out_weight, self.mlp_out_min, self.mlp_out_max = utils.u8_encode(self.mlp_out_weight)
        if self.neuron_config and self.neuron_config.quant:
            if self.mlp_in_weight is not None:
                self.mlp_in_weight, self.mlp_in_scales = \
                    quantize.maybe_quantize_weights(self.mlp_in_weight, self.neuron_config.quant)
                self.mlp_out_weight, self.mlp_out_scales = \
                    quantize.maybe_quantize_weights(self.mlp_out_weight, self.neuron_config.quant,
                                                    out_feature_dim = 1 if self.mlp_out_transposed else 0)

            if self.neuron_config.quant.quantize_attn:
                if self.neuron_config.fuse_qkv:
                    fused_qkv_weight, fused_qkv_scales = \
                        quantize.maybe_quantize_weights(fused_qkv_weight, self.neuron_config.quant)
                else:
                    self.attn_q_weight, self.attn_q_scales = \
                        quantize.maybe_quantize_weights(self.attn_q_weight, self.neuron_config.quant)
                    self.attn_k_weight, self.attn_k_scales = \
                        quantize.maybe_quantize_weights(self.attn_k_weight, self.neuron_config.quant)
                    self.attn_v_weight, self.attn_v_scales = \
                        quantize.maybe_quantize_weights(self.attn_v_weight, self.neuron_config.quant)
                self.attn_out_weight, self.attn_out_scales = \
                    quantize.maybe_quantize_weights(self.attn_out_weight, self.neuron_config.quant,
                                                    out_feature_dim = 1 if self.attn_out_transposed else 0)


        maybe_manipulator = MaybeParallelTensorManipulator(self.tp_degree)
        maybe_duplicate = maybe_manipulator.duplicate
        maybe_shard_along = maybe_manipulator.shard_along
        maybe_primary_only = maybe_manipulator.primary_only
        maybe_shard_along_and_transform = maybe_manipulator.shard_along_and_transform
        self.pre_attn_ln_weight = maybe_duplicate(self.pre_attn_ln_weight)
        self.pre_attn_ln_bias = maybe_duplicate(self.pre_attn_ln_bias)
        if self.neuron_config and self.neuron_config.fuse_qkv:
            self.attn_q_weight = maybe_shard_along(fused_qkv_weight, dim=1)
            self.attn_q_bias = maybe_shard_along(fused_qkv_bias, dim=0)
            self.attn_q_scales = maybe_shard_along(fused_qkv_scales, dim=0)
        else:
            self.attn_q_weight = maybe_shard_along(self.attn_q_weight, dim=1)
            self.attn_q_scales = maybe_shard_along(self.attn_q_scales, dim=0)
            self.attn_q_bias = maybe_shard_along(self.attn_q_bias, dim=0)
        self.attn_k_weight = maybe_shard_along(self.attn_k_weight, dim=1)
        self.attn_k_scales = maybe_shard_along(self.attn_k_scales, dim=0)
        self.attn_k_bias = maybe_shard_along(self.attn_k_bias, dim=0)
        self.attn_v_weight = maybe_shard_along(self.attn_v_weight, dim=1)
        self.attn_v_scales = maybe_shard_along(self.attn_v_scales, dim=0)
        self.attn_v_bias = maybe_shard_along(self.attn_v_bias, dim=0)
        self.attn_out_weight = maybe_shard_along(self.attn_out_weight, dim=self.attn_out_sharding)
        self.attn_out_scales = maybe_duplicate(self.attn_out_scales)
        self.attn_out_bias = maybe_primary_only(self.attn_out_bias)
        self.post_attn_ln_weight = maybe_duplicate(self.post_attn_ln_weight)
        self.post_attn_ln_bias = maybe_duplicate(self.post_attn_ln_bias)
        self.pre_mlp_ln_weight = maybe_duplicate(self.pre_mlp_ln_weight)
        self.pre_mlp_ln_bias = maybe_duplicate(self.pre_mlp_ln_bias)
        if self.mlp_in_weight is not None:
            self.mlp_in_weight = maybe_shard_along_and_transform(self.mlp_in_weight, 1)
            self.mlp_in_scales = maybe_shard_along(self.mlp_in_scales, dim=0)
            self.mlp_in_bias = maybe_shard_along(self.mlp_in_bias, dim=0)
            self.mlp_out_weight = maybe_shard_along_and_transform(self.mlp_out_weight, dim=self.mlp_out_sharding)
            self.mlp_out_scales = maybe_duplicate(self.mlp_out_scales)
            self.mlp_out_bias = maybe_primary_only(self.mlp_out_bias)
        self.post_mlp_ln_weight = maybe_duplicate(self.post_mlp_ln_weight)
        self.post_mlp_ln_bias = maybe_duplicate(self.post_mlp_ln_bias)

        extras = []
        for param, dim, allow_pad, allow_quantize, out_feature_dim, allow_transform in self.extra_parameters:
            if allow_pad:
                size = utils.round_up_to_divisor(param.shape[dim], self.tp_degree)
                if os.environ.get("NEURON_INTERNAL_TRANSFORM_WEIGHT_LAYOUT", None) and allow_transform:
                    size = utils.round_up_to_divisor(size // self.tp_degree,
                                                     constants.TILE_SIZE) * self.tp_degree
                param = utils.pad(param, dim, size)

            if allow_quantize:
                # If the parameter is quantizable and the quantization is enabled, we calculate the
                # scaling factors here, otherwise we still need to add a scale placeholder to match
                # the layer arguments
                if self.neuron_config and self.neuron_config.quant:
                    param, scales = quantize.maybe_quantize_weights(param, self.neuron_config.quant,
                                                                    out_feature_dim=out_feature_dim)
                    scales_dim = 0 if dim == out_feature_dim else None
                    scales = maybe_manipulator.duplicate_or_shard_along(scales, scales_dim)
                else:
                    scales = None

            if allow_transform:
                param = maybe_shard_along_and_transform(param, dim)
            else:
                param = maybe_manipulator.duplicate_or_shard_along(param, dim)

            extras.append(param)
            if allow_quantize:
                extras.append(scales)

        self.extra_parameters = extras

        self.init_caches()

    def init_caches(self):
        n_heads_kv_cache = self.n_kv_head

        # When padding, compute the hidden size based on the padding. We must
        # allow the KV cache to be padded so it can be evenly divisible across
        # NeuronCores.
        if self.allow_pad and not self.shard_over_batch:
            n_heads_kv_cache = utils.round_up_to_divisor(self.n_kv_head, self.tp_degree)
        # Separate KV cache for each batch size
        manipulator = parallel.ParallelTensorManipulator(self.tp_degree)
        for batch_size in self.batch_sizes:
            cache_shape = [self.n_positions, batch_size, n_heads_kv_cache, self.attention_head_size]
            cpu_cache = torch.zeros(cache_shape, dtype=self.cache_dtype)
            if self.shard_over_batch:
                self.cache_shape[batch_size] = [self.n_positions, batch_size // self.tp_degree, n_heads_kv_cache, self.attention_head_size]
                self.attn_k_cache[batch_size] = (manipulator.shard_along(cpu_cache, dim=1))
                self.attn_v_cache[batch_size] = (manipulator.shard_along(cpu_cache, dim=1))
            else:
                assert (n_heads_kv_cache >= self.tp_degree) and (n_heads_kv_cache % self.tp_degree == 0), \
                    f"cannot shard along kv_heads dimension: n_kv_head={n_heads_kv_cache}, tp_degree={self.tp_degree}"
                self.cache_shape[batch_size] = [self.n_positions, batch_size, n_heads_kv_cache // self.tp_degree, self.attention_head_size]
                self.attn_k_cache[batch_size] = (manipulator.shard_along(cpu_cache, dim=2))
                self.attn_v_cache[batch_size] = (manipulator.shard_along(cpu_cache, dim=2))

    def assign_caches(self, layer, buckets_from_src=False):
        batch_sizes = self.batch_sizes
        if buckets_from_src:
            # In continuous batching, we exclusively use batch_size=1 for parallel context encoding.
            # But still use all batch_sizes for decoding.
            batch_sizes = layer.batch_sizes
        for batch_size in batch_sizes:
            self.attn_k_cache[batch_size] = layer.attn_k_cache[batch_size]
            self.attn_v_cache[batch_size] = layer.attn_v_cache[batch_size]
            self.cache_shape[batch_size] = layer.cache_shape[batch_size]

    def all_parameters(self):
        return [
            self.pre_attn_ln_weight,
            self.pre_attn_ln_bias,
            self.attn_q_weight,
            self.attn_q_scales,
            self.attn_q_bias,
            self.attn_k_weight,
            self.attn_k_scales,
            self.attn_k_bias,
            self.attn_v_weight,
            self.attn_v_scales,
            self.attn_v_bias,
            self.attn_out_weight,
            self.attn_out_scales,
            self.attn_out_bias,
            self.post_attn_ln_weight,
            self.post_attn_ln_bias,
            self.pre_mlp_ln_weight,
            self.pre_mlp_ln_bias,
            self.mlp_in_weight,
            self.mlp_in_scales,
            self.mlp_in_bias,
            self.mlp_out_weight,
            self.mlp_out_scales,
            self.mlp_out_bias,
            self.post_mlp_ln_weight,
            self.post_mlp_ln_bias,
            *self.extra_parameters,
        ]

    def valid_parameters(self):
        return [par for par in self.all_parameters() if par is not None]

    def u8_bounds(self):
        bounds = (
            self.attn_q_min, self.attn_q_max, self.attn_k_min, self.attn_k_max,
            self.attn_v_min, self.attn_v_max, self.attn_out_min, self.attn_out_max,
            self.mlp_in_min, self.mlp_in_max, self.mlp_out_min, self.mlp_out_max,
        )
        if any(bd is None for bd in bounds):
            return None
        return bounds

    def hlo_maybe_dequantize_weights(self, hlo_weights):
        u8_bounds = self.u8_bounds()
        if u8_bounds is None:
            return hlo_weights
        first_valid_weight, *_ = [weight for weight in hlo_weights if weight is not None]
        scribe = first_valid_weight.scribe
        amp, quantized, dequantized = utils.parse_amp(self.amp)
        dtype = getattr(scribe, amp)
        dequant_dtype = None if dequantized is None else getattr(scribe, dequantized)

        def attn_u8_decode(q_weight, k_weight, v_weight, out_weight, u8_bounds):
            q_min, q_max, k_min, k_max, v_min, v_max, out_min, out_max, *_ = u8_bounds
            q_weight = hlo.u8_decode(dtype, dequant_dtype, q_weight, q_min, q_max)
            k_weight = hlo.u8_decode(dtype, dequant_dtype, k_weight, k_min, k_max)
            v_weight = hlo.u8_decode(dtype, dequant_dtype, v_weight, v_min, v_max)
            out_weight = hlo.u8_decode(dtype, dequant_dtype, out_weight, out_min, out_max)
            return q_weight, k_weight, v_weight, out_weight

        def mlp_u8_decode(in_weight, out_weight, u8_bounds):
            *_, in_min, in_max, out_min, out_max = u8_bounds
            in_weight = hlo.u8_decode(dtype, dequant_dtype, in_weight, in_min, in_max)
            out_weight = hlo.u8_decode(dtype, dequant_dtype, out_weight, out_min, out_max)
            return in_weight, out_weight

        (
            pre_attn_ln_weight,
            pre_attn_ln_bias,
            attn_q_weight,
            attn_q_scales,
            attn_q_bias,
            attn_k_weight,
            attn_k_scales,
            attn_k_bias,
            attn_v_weight,
            attn_v_scales,
            attn_v_bias,
            attn_out_weight,
            attn_out_scales,
            attn_out_bias,
            post_attn_ln_weight,
            post_attn_ln_bias,
            pre_mlp_ln_weight,
            pre_mlp_ln_bias,
            mlp_in_weight,
            mlp_in_scales,
            mlp_in_bias,
            mlp_out_weight,
            mlp_out_scales,
            mlp_out_bias,
            post_mlp_ln_weight,
            post_mlp_ln_bias,
        ) = hlo_weights
        attn_q_weight, attn_k_weight, attn_v_weight, attn_out_weight = attn_u8_decode(
            attn_q_weight, attn_k_weight, attn_v_weight, attn_out_weight, u8_bounds)
        mlp_in_weight, mlp_out_weight = mlp_u8_decode(mlp_in_weight, mlp_out_weight, u8_bounds)
        return [
            pre_attn_ln_weight,
            pre_attn_ln_bias,
            attn_q_weight,
            attn_q_scales,
            attn_q_bias,
            attn_k_weight,
            attn_k_scales,
            attn_k_bias,
            attn_v_weight,
            attn_v_scales,
            attn_v_bias,
            attn_out_weight,
            attn_out_scales,
            attn_out_bias,
            post_attn_ln_weight,
            post_attn_ln_bias,
            pre_mlp_ln_weight,
            pre_mlp_ln_bias,
            mlp_in_weight,
            mlp_in_scales,
            mlp_in_bias,
            mlp_out_weight,
            mlp_out_scales,
            mlp_out_bias,
            post_mlp_ln_weight,
            post_mlp_ln_bias,
        ]

    def reset(self):
        for batch_size in self.batch_sizes:
            zero_cache = torch.zeros(self.attn_k_cache[batch_size].shape, dtype=self.attn_k_cache[batch_size].dtype)
            zero_cache = [zero_cache for _ in range(self.tp_degree)]
            ops.parallel_write(self.attn_k_cache[batch_size], zero_cache)
            ops.parallel_write(self.attn_v_cache[batch_size], zero_cache)

    def assign_parameters(self, layer):
        self.pre_attn_ln_weight = layer.pre_attn_ln_weight
        self.pre_attn_ln_bias = layer.pre_attn_ln_bias
        self.attn_q_weight = layer.attn_q_weight
        self.attn_q_scales = layer.attn_q_scales
        self.attn_q_bias = layer.attn_q_bias
        self.attn_k_weight = layer.attn_k_weight
        self.attn_k_scales = layer.attn_k_scales
        self.attn_k_bias = layer.attn_k_bias
        self.attn_v_weight = layer.attn_v_weight
        self.attn_v_scales = layer.attn_v_scales
        self.attn_v_bias = layer.attn_v_bias
        self.attn_out_weight = layer.attn_out_weight
        self.attn_out_scales = layer.attn_out_scales
        self.attn_out_bias = layer.attn_out_bias
        self.post_attn_ln_weight = layer.post_attn_ln_weight
        self.post_attn_ln_bias = layer.post_attn_ln_bias
        self.pre_mlp_ln_weight = layer.pre_mlp_ln_weight
        self.pre_mlp_ln_bias = layer.pre_mlp_ln_bias
        self.mlp_in_weight = layer.mlp_in_weight
        self.mlp_in_scales = layer.mlp_in_scales
        self.mlp_in_bias = layer.mlp_in_bias
        self.mlp_out_weight = layer.mlp_out_weight
        self.mlp_out_scales = layer.mlp_out_scales
        self.mlp_out_bias = layer.mlp_out_bias
        self.post_mlp_ln_weight = layer.post_mlp_ln_weight
        self.post_mlp_ln_bias = layer.post_mlp_ln_bias
        self.attn_q_min = layer.attn_q_min
        self.attn_q_max = layer.attn_q_max
        self.attn_k_min = layer.attn_k_min
        self.attn_k_max = layer.attn_k_max
        self.attn_v_min = layer.attn_v_min
        self.attn_v_max = layer.attn_v_max
        self.attn_out_min = layer.attn_out_min
        self.attn_out_max = layer.attn_out_max
        self.mlp_in_min = layer.mlp_in_min
        self.mlp_in_max = layer.mlp_in_max
        self.mlp_out_min = layer.mlp_out_min
        self.mlp_out_max = layer.mlp_out_max
        self.extra_parameters = layer.extra_parameters

class MaybeParallelTensorManipulator:

    def __init__(self, tp_degree):
        self.manipulator = parallel.ParallelTensorManipulator(tp_degree)

    def duplicate(self, tensor):
        if tensor is None:
            return None
        return self.manipulator.duplicate(tensor)

    def shard_along(self, tensor, dim):
        if tensor is None:
            return None
        return self.manipulator.shard_along(tensor, dim)

    def primary_only(self, tensor):
        if tensor is None:
            return None
        return self.manipulator.primary_only(tensor)

    def duplicate_or_shard_along(self, tensor, dim):
        if dim is None:
            return self.duplicate(tensor)
        return self.shard_along(tensor, dim)

    def transform_and_tile_weight_layout(self, tensors):
        if tensors is None:
            return None

        if os.environ.get("NEURON_INTERNAL_TRANSFORM_WEIGHT_LAYOUT", None):
            new_tensors = []
            for tensor in tensors:
                K, N = tensor.shape
                assert(K % constants.TILE_SIZE == 0 and N % constants.TILE_SIZE == 0)
                reshape_sizes = [K // constants.TILE_SIZE,
                                 constants.TILE_SIZE,
                                 N // constants.TILE_SIZE,
                                 constants.TILE_SIZE]
                tensor = tensor.reshape(reshape_sizes) \
                               .permute([1, 2, 0, 3])
                tensor = tensor.contiguous()
                new_tensors.append(tensor)
            return new_tensors

        return tensors

    def shard_along_and_transform(self, tensor, dim):
        if tensor is None:
            return None
        tensors = self.manipulator.shard_along_on_cpu(tensor, dim)
        tensors = self.transform_and_tile_weight_layout(tensors)
        tensor = ops.parallel_to_nc(tensors)
        return tensor

class DecoderParameterBuilder:

    def __init__(self, scribe, parameter_number):
        self.scribe = scribe
        self.parameter_number = parameter_number
        self.dtype_converter = compiler.DataTypeConverter()

    def from_tensor(self, tensor, dim_size=None):
        if tensor is None:
            return None
        name = self.dtype_converter.torch2name(tensor.dtype)
        dtype = getattr(self.scribe, name)
        sizes = list(tensor.shape)
        if dim_size is not None:
            for dim, size in dim_size.items():
                sizes[dim] = size
        param = dtype[sizes].Parameter(parameter_number=self.parameter_number)
        self.parameter_number += 1
        return param

class DecoderProgram:

    def __init__(self, layers, hlo_modules : dict, num_inputs, tp_degree, n_positions_list, batch_sizes, prefixed_length=0, batch_size_for_shared_caches=False):
        # Each hlo module corresponds to one npos and one batch_size
        # hlo_modules is a 2D map (i,j) i is npos , j is batch_size
        self.layers = layers
        self.batch_sizes = batch_sizes
        self.batch_size_for_shared_caches = batch_size_for_shared_caches
        self.n_positions_list = n_positions_list
        self.prefixed_length = prefixed_length
        first_hlo = hlo_modules[self.n_positions_list[0], self.batch_sizes[0]]
        hlos_for_input = list()
        hlos_for_input = [hlo_modules[self.n_positions_list[0],batch_size] for batch_size in self.batch_sizes]
        self.input_buffers = list()
        self.input_buffers = [[compiler.gen_zero_input(hlo,idx) for idx in range(num_inputs)] for hlo in hlos_for_input]
        self.kernels = dict()
        for npos, batch_size in itertools.product(self.n_positions_list, self.batch_sizes):
            self.kernels[npos,batch_size] = compiler.ParallelKernel(hlo_modules[npos, batch_size], tp_degree)
        # self.n_positions_list = [read_n_position(hm, num_inputs) for hm in hlo_modules]
        self.n_active_tokens = read_n_active_tokens(first_hlo)
        self.manipulator = parallel.ParallelTensorManipulator(tp_degree)
        self.tp_degree = tp_degree
        self.need_reorder_cache = False

    def setup(self, layers, ln_lm_head_params, io_ring_cache_size=1):
        self.input_buffers = [[self.manipulator.duplicate(buf) for buf in input_buffers_for_batch_size] for input_buffers_for_batch_size in self.input_buffers]
        self.logits_buffer = [self.manipulator.duplicate(buf) for buf in self.logits_buffer]
        # Compile modules in parallel
        with ProcessPoolExecutor(max_workers=len(self.n_positions_list) * len(self.batch_sizes)) as executor:
            neff_bytes_futures = dict()
            for npos_bs_tuple, kernel in self.kernels.items():
                future = executor.submit(kernel.compile, npos_bs_tuple[0])
                neff_bytes_futures[npos_bs_tuple] = future
            for npos_bs_tuple, kernel in self.kernels.items():
                kernel.neff_bytes = neff_bytes_futures[npos_bs_tuple].result()
        for kernel in self.kernels.values():
            kernel.load(io_ring_cache_size)

    def setup_reorder_cache(self, also_compile_now=True):
        self.need_reorder_cache = True
        self.reorder_cache_hlo_kernels = [self._create_reoder_cache_kernel(batch_size) for batch_size in self.batch_sizes]
        if also_compile_now:
            self.setup_reorder_cache_kernels()


    def find_bucket_id(self, length):
        return next(idx for idx, npos in enumerate(self.n_positions_list) if npos >= length+1)

    def inputs_host_to_device(self, input_tensors, batch_size):
        input_buffers = self.input_buffers[self.batch_sizes.index(batch_size)]
        for buf, tensor in zip(input_buffers, input_tensors):
            assert buf.shape == tensor.shape, f"Copying tensor from host to device: buffer ({buf.shape}) and tensor ({tensor.shape}) have different shapes!"
            tensor = tensor.to(buf.dtype)
            tensor = self.manipulator.duplicate_on_cpu(tensor)
            ops.parallel_write(buf, tensor)

    def run(self, bucket_id):
        raise NotImplementedError(DecoderProgram)

    def logits_device_to_host(self, batch_size):
        idx = self.batch_sizes.index(batch_size)
        return self.manipulator.unshard_along(self.logits_buffer[idx], dim=0)

    def _fill_io_tensors(self, input_tensors, output_tensors, layers, npos, batch_size):
        end = npos
        if self.prefixed_length > 0:
            end = npos + self.prefixed_length
        if self.batch_size_for_shared_caches:
            batch_size = self.batch_size_for_shared_caches
        for layer in layers:
            for cache in layer.attn_k_cache[batch_size], layer.attn_v_cache[batch_size]:
                cache_slice = self.manipulator.slice_on_nc(cache, 0, start=0, end=end, step=1)
                input_tensors.append(cache_slice)
                output_tensors.append(cache_slice)
        for layer in layers:
            input_tensors.extend(layer.valid_parameters())

    def _create_reoder_cache_kernel(self, batch_size):
        # assume each layer have same size of cache
        def _reorder_cache(scribe):
            reorder_ids = scribe.s64[batch_size].Parameter(parameter_number=0)
            caches = []
            param_builder = DecoderParameterBuilder(scribe, 1)
            for layer in self.layers:
                for cache in layer.attn_k_cache[batch_size], layer.attn_v_cache[batch_size]:
                    cache = param_builder.from_tensor(cache)
                    caches.append(cache)
            outputs = []
            # TODO: concat -> reorder -> indexing?
            # cache of shape [self.n_positions, self.batch_size, n_heads_kv_cache//self.tp_degree, self.attention_head_size]
            # we want to reorder on batch dimension
            for cache in caches:
                new_cache = hlo.index_select(cache, 1, reorder_ids)
                outputs.append(new_cache)
            root_shapes = [tensor.dtype[tensor.sizes] for tensor in outputs]
            return scribe.tuple(*root_shapes).Tuple(*outputs)

        return compiler.HLOKernel(_reorder_cache, self.tp_degree)

    def setup_reorder_cache_kernels(self):
        for bs_idx, batch_size in  enumerate(self.batch_sizes):
            reorder_cache_hlo_kernel = self.reorder_cache_hlo_kernels[bs_idx]
            self._setup_reorder_cache_kernel(reorder_cache_hlo_kernel, batch_size)

    def _setup_reorder_cache_kernel(self, reorder_cache_hlo_kernel, batch_size):
        reorder_cache_hlo_kernel.build()
        reorder_cache_hlo_kernel.load()
        # setup memory buffer
        reorder_ids = torch.zeros(self.layers[0].cache_shape[batch_size], dtype=torch.int64)
        self.reorder_ids_buffers = reorder_cache_hlo_kernel.manipulator.duplicate(reorder_ids)
        input_tensors = [self.reorder_ids_buffers]
        output_tensors = []
        for layer in self.layers:
            for cache in layer.attn_k_cache[batch_size], layer.attn_v_cache[batch_size]:
                input_tensors.append(cache)
                output_tensors.append(cache) # aliasing
        reorder_cache_hlo_kernel.setup(input_tensors, output_tensors)

    def reorder_cache_by_batch_size(self, reorder_ids, batch_size):
        assert self.need_reorder_cache, "DecoderProgram is not built with reorder_cache"
        reorder_ids_tensor = torch.tensor(reorder_ids, dtype=torch.int64)
        # TODO: if reorder_ids == range(batch_size), don't do anything
        idx = self.batch_sizes.index(batch_size)
        reorder_ids_tensors_cpu = self.reorder_cache_hlo_kernels[idx].manipulator.duplicate_on_cpu(reorder_ids_tensor)
        ops.parallel_write(self.reorder_ids_buffers, reorder_ids_tensors_cpu)
        self.reorder_cache_hlo_kernels[idx].run()

    def reorder_cache(self, reorder_ids):
        assert self.need_reorder_cache, "DecoderProgram is not built with reorder_cache"
        reorder_ids_tensor = torch.tensor(reorder_ids, dtype=torch.int64)
        # TODO: if reorder_ids == range(batch_size), don't do anything
        for bs_idx, batch_size in enumerate(self.batch_sizes):
            reorder_ids_tensors_cpu = self.reorder_cache_hlo_kernels[bs_idx].manipulator.duplicate_on_cpu(reorder_ids_tensor)
            ops.parallel_write(self.reorder_ids_buffers, reorder_ids_tensors_cpu)
            self.reorder_cache_hlo_kernels[bs_idx].run()

    def get_kernels(self):
        all_kernels = list()
        for npos, batch_size in itertools.product(self.n_positions_list, self.batch_sizes):
            all_kernels.append(self.kernels[npos,batch_size])
        return all_kernels


class DecoderProgramFullyUnrolled(DecoderProgram):

    def __init__(self, layers, hlo_modules, num_inputs, tp_degree, n_positions_list, batch_sizes, prefixed_length=0, batch_size_for_shared_caches=None):
        super().__init__(layers, hlo_modules, num_inputs, tp_degree, n_positions_list, batch_sizes, prefixed_length, batch_size_for_shared_caches)
        hlos_for_input = list()
        hlos_for_input = [hlo_modules[self.n_positions_list[0],batch_size] for batch_size in self.batch_sizes]
        self.logits_buffer = [compiler.gen_zero_output(hlo, 0) for hlo in hlos_for_input]
        self.memories = dict()
        self.executors = dict()
        for npos,batch_size in itertools.product(self.n_positions_list, self.batch_sizes):
            self.memories[npos,batch_size] = self.kernels[npos,batch_size].build_memory()

    def setup(self, layers, ln_lm_head_params):
        super().setup(layers, ln_lm_head_params)
        # Setup the memory with input and output buffers
        for bs_idx, batch_size in enumerate(self.batch_sizes):
            for npos in self.n_positions_list:
                input_tensors = [*self.input_buffers[bs_idx]]
                output_tensors = [self.logits_buffer[bs_idx]]
                self._fill_io_tensors(input_tensors, output_tensors, layers, npos, batch_size)
                input_tensors.extend(ln_lm_head_params)
                self.memories[npos,batch_size].setup(input_tensors, output_tensors)

    def run(self, bucket_id, batch_size):
        npos = self.n_positions_list[bucket_id]
        self.kernels[npos,batch_size](self.memories[npos,batch_size])

    def enable_executor(self):
        for bs_idx, batch_size in enumerate(self.batch_sizes):
            for npos in self.n_positions_list:
                input_tensors = [*self.input_buffers[bs_idx]]
                output_tensors = [self.logits_buffer[bs_idx]]
                executor = self.kernels[npos,batch_size].build_executor(self.memories[npos,batch_size], input_tensors, output_tensors)
                self.executors[npos,batch_size] = executor

    def execute(self, bucket_id, batch_size, *inputs, return_ranks=-1):
        """
        Execute a kernel with using the optimized ParallelExecutor.

        This is an alternative to the `run` method which requires that an
        executor has been constructed for each of the underlying kernels.

        Arguments:
            bucket_id: The kernel bucket to execute
            inputs: The set of CPU tensors to copy to each model
            return_ranks: The number of ranks to copy back to CPU
        """
        npos = self.n_positions_list[bucket_id]
        return self.executors[npos,batch_size](inputs, return_ranks)

    def get_kernels(self):
        all_kernels = super().get_kernels()
        # only true when reorder_cache called before to_neuron
        if self.need_reorder_cache:
            for hlo_kernel in self.reorder_cache_hlo_kernels:
                all_kernels.append(hlo_kernel.kernel)
        return all_kernels

class DecoderProgramMultiLayer(DecoderProgram):

    def __init__(self, layers, hlo_modules, ln_lm_head_hlo_modules, num_inputs, num_layers, unroll, tp_degree, n_positions_list, batch_sizes, prefixed_length=0):
        super().__init__(layers, hlo_modules, num_inputs, tp_degree, n_positions_list, batch_sizes, prefixed_length)
        if num_layers % unroll:
            raise ValueError(f'unroll={unroll} does not divide num_layers={num_layers}')
        assert len(ln_lm_head_hlo_modules) == len(batch_sizes)
        self.logits_buffer = [compiler.gen_zero_output(hm) for hm in ln_lm_head_hlo_modules]
        self.unroll = unroll
        self.multi_layers_memories = []
        for _ in range(num_layers // unroll):
            memories = dict()
            for npos, batch_size in itertools.product(self.n_positions_list, self.batch_sizes):
                memories[npos,batch_size] = self.kernels[npos,batch_size].build_memory()
            self.multi_layers_memories.append(memories)
        self.ln_lm_head_kernels = [compiler.ParallelKernel(hm, tp_degree) for hm in ln_lm_head_hlo_modules]
        self.ln_lm_head_memories = [ln_lm_head_kernel.build_memory() for ln_lm_head_kernel in self.ln_lm_head_kernels]
        self.layer_executors = list()
        self.lm_head_executors = list()

    def setup(self, layers, ln_lm_head_params):
        super().setup(layers, ln_lm_head_params, io_ring_cache_size=len(self.multi_layers_memories))
        hidden_buffers = list()
        last_token_id_buffers = list()
        for input_buffer in self.input_buffers:
            hidden_buffer, *_, last_token_id_buffer, _ = input_buffer
            hidden_buffers.append(hidden_buffer)
            last_token_id_buffers.append(last_token_id_buffer)

        multi_layer_starts = range(0, len(layers), self.unroll)
        multi_layers = [layers[start:start+self.unroll] for start in multi_layer_starts]

        for multi_layer_idx, multi_layer in enumerate(multi_layers):
            multi_layer_memory = self.multi_layers_memories[multi_layer_idx]
            for bs_idx, batch_size in enumerate(self.batch_sizes):
                for npos in self.n_positions_list:
                    input_tensors = [*self.input_buffers[bs_idx]]
                    output_tensors = [hidden_buffers[bs_idx]]
                    self._fill_io_tensors(input_tensors, output_tensors, multi_layer, npos, batch_size)
                    multi_layer_memory[npos,batch_size].setup(input_tensors, output_tensors)

        for head_idx in range(0,len(self.ln_lm_head_kernels)):
            self.ln_lm_head_memories[head_idx].setup([hidden_buffers[head_idx], last_token_id_buffers[head_idx], *ln_lm_head_params], [self.logits_buffer[head_idx]])
            self.ln_lm_head_kernels[head_idx].build()
            self.ln_lm_head_kernels[head_idx].load()

    def run(self, bucket_id, batch_size):
        npos = self.n_positions_list[bucket_id]
        for memories in self.multi_layers_memories:
            self.kernels[npos,batch_size](memories[npos,batch_size])
        bs_idx = self.batch_sizes.index(batch_size)
        self.ln_lm_head_kernels[bs_idx](self.ln_lm_head_memories[bs_idx])

    def enable_executor(self):
        for layer_memories in self.multi_layers_memories:
            executors = dict()
            self.layer_executors.append(executors)
            for npos, batch_size in itertools.product(self.n_positions_list, self.batch_sizes):
                executors[npos,batch_size] = self.kernels[npos,batch_size].build_executor(layer_memories[npos,batch_size], [], [])
        # head
        for idx, kernel in enumerate(self.ln_lm_head_kernels):
            output_tensors = [self.logits_buffer[idx]]
            executor = kernel.build_executor(self.ln_lm_head_memories[idx], [], output_tensors)
            self.lm_head_executors.append(executor)

    def execute(self, bucket_id, batch_size, *inputs, return_ranks=-1):
        self.inputs_host_to_device(inputs, batch_size) # One-time input copy
        npos = self.n_positions_list[bucket_id]
        for layer_executor in self.layer_executors:
            layer_executor[npos,batch_size]([], return_ranks=0)
        return self.lm_head_executors[self.batch_sizes.index(batch_size)]([], return_ranks=return_ranks)

    def get_kernels(self):
        all_kernels = super().get_kernels()
        # Head kernel
        for kernel in self.ln_lm_head_kernels:
            all_kernels.append(kernel)
        # only true when reorder_cache called before to_neuron
        if self.need_reorder_cache:
            for hlo_kernel in self.reorder_cache_hlo_kernels:
                all_kernels.append(hlo_kernel.kernel)
        return all_kernels

class FastCacheBroadcaster(base.NeuronBaseSerializer):

    def __init__(self, n_positions, from_batch_size, to_batch_size, n_heads_tp, d_head, amp,
                 tp_degree, n_layer):
        cache_broadcast_impl = hlo.cache_broadcast(n_positions, from_batch_size, to_batch_size,
                                                   n_heads_tp, d_head, amp, n_layer)
        cache_broadcast_hlo_module = compiler.compile_py_func(cache_broadcast_impl)
        self.cache_broadcast_kernel = compiler.ParallelKernel(cache_broadcast_hlo_module, tp_degree)
        self.cache_broadcast_memory = self.cache_broadcast_kernel.build_memory()

    def build(self):
        if self.compiler_artifacts_path is not None:
            self.set_neff_bytes()
        self.cache_broadcast_kernel.build()

    def load(self):
        self.cache_broadcast_kernel.load()

    def setup(self, source_caches, target_caches):
        self.cache_broadcast_memory.setup(source_caches, target_caches)

    def run_broadcast(self):
        self.cache_broadcast_kernel(self.cache_broadcast_memory)

    def get_all_kernels(self):
	    return [self.cache_broadcast_kernel]
