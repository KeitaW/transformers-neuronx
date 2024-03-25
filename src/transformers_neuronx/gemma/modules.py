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
from transformers_neuronx import dtypes
from transformers_neuronx import module
from transformers_neuronx import utils


class GemmaForCausalLM(module.PretrainedModel):

    def __init__(self, config):
        super().__init__()
        dtype, _, _ = utils.parse_amp(config.amp)
        dtype = dtypes.to_torch_dtype(dtype)
        self.model = GemmaModel(config)
        self.lm_head = module.LowMemoryLazyLinear(config.vocab_size, dtype=dtype, bias=False)


class GemmaModel(module.LowMemoryModule):

    def __init__(self, config):
        super().__init__()
        self.embed_tokens = module.LowMemoryEmbedding(config.vocab_size, config.hidden_size)
        self.layers = module.LowMemoryModuleList([GemmaDecoderLayer(config) for _ in range(config.num_hidden_layers)])
        self.norm = GemmaRMSNorm(config)


class GemmaRMSNorm(module.LowMemoryModule):

    def __init__(self, config) -> None:
        super().__init__()
        self.weight = module.UninitializedParameter()


class GemmaDecoderLayer(module.LowMemoryModule):

    def __init__(self, config):
        super().__init__()
        self.self_attn = GemmaAttention(config)
        self.mlp = GemmaMLP(config)
        self.input_layernorm = GemmaRMSNorm(config)
        self.post_attention_layernorm = GemmaRMSNorm(config)


class GemmaAttention(module.LowMemoryModule):

    def __init__(self, config):
        super().__init__()
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.hidden_size // self.num_heads
        dtype, _, _ = utils.parse_amp(config.amp)
        dtype = dtypes.to_torch_dtype(dtype)
        self.q_proj = module.LowMemoryLazyLinear(self.num_heads * self.head_dim, bias=False, dtype=dtype)
        self.k_proj = module.LowMemoryLazyLinear(self.num_heads * self.head_dim, bias=False, dtype=dtype)
        self.v_proj = module.LowMemoryLazyLinear(self.num_heads * self.head_dim, bias=False, dtype=dtype)
        self.o_proj = module.LowMemoryLazyLinear(self.hidden_size, bias=False, dtype=dtype)


class GemmaMLP(module.LowMemoryModule):

    def __init__(self, config):
        super().__init__()
        dtype, _, _ = utils.parse_amp(config.amp)
        dtype = dtypes.to_torch_dtype(dtype)
        self.gate_proj = module.LowMemoryLazyLinear(config.intermediate_size, bias=False, dtype=dtype)
        self.up_proj = module.LowMemoryLazyLinear(config.intermediate_size, bias=False, dtype=dtype)
        self.down_proj = module.LowMemoryLazyLinear(config.hidden_size, bias=False, dtype=dtype)
