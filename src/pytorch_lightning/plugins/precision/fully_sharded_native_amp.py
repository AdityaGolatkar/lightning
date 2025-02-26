# Copyright The PyTorch Lightning team.
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
from typing import Any, Optional

import torch

from pytorch_lightning.plugins.precision.sharded_native_amp import ShardedNativeMixedPrecisionPlugin
from pytorch_lightning.utilities.enums import PrecisionType
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.imports import _TORCH_GREATER_EQUAL_1_12

if _TORCH_GREATER_EQUAL_1_12:
    from torch.distributed.fsdp.fully_sharded_data_parallel import MixedPrecision
else:
    MixedPrecision = None  # type: ignore[misc,assignment]


class FullyShardedNativeMixedPrecisionPlugin(ShardedNativeMixedPrecisionPlugin):
    """Native AMP for Fully Sharded Training."""

    def clip_grad_by_norm(self, *_: Any, **__: Any) -> None:
        # see https://fairscale.readthedocs.io/en/latest/api/nn/fsdp.html
        # section `Gradient Clipping`, using `torch.nn.utils.clip_grad_norm_` is incorrect
        # for FSDP module. To overcome this, needs to call sharded_module.clip_grad_norm(clip_val)
        # however we rely on LightningModule's configure_sharded_model to wrap FSDP, it would be hard to
        # trace back the root FSDP. Now we only support clip by value.
        raise MisconfigurationException(
            f"`gradient_clip_algorithm='norm'` is currently not supported for `{self.__class__.__name__}`"
        )

    @property
    def mixed_precision_config(self) -> Optional[MixedPrecision]:
        assert MixedPrecision is not None
        if self.precision == PrecisionType.HALF:
            dtype = torch.float16
        elif self.precision == PrecisionType.BFLOAT:
            dtype = torch.bfloat16
        else:
            raise MisconfigurationException(f"Was unable to infer precision type, received {self.precision!r}.")
        return MixedPrecision(
            param_dtype=dtype,
            reduce_dtype=dtype,
            buffer_dtype=dtype,
        )
