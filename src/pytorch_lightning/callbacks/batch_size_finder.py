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
r"""
BatchSizeFinder
===============

Finds optimal batch size
"""

import logging
import os
import uuid
from copy import deepcopy
from typing import List, Optional, Tuple, TYPE_CHECKING, Union

from torch.utils.data.dataloader import DataLoader
from typing_extensions import TypedDict

import pytorch_lightning as pl
from pytorch_lightning.callbacks.base import Callback
from pytorch_lightning.utilities.exceptions import _TunerExitException, MisconfigurationException
from pytorch_lightning.utilities.memory import garbage_collection_cuda, is_oom_error
from pytorch_lightning.utilities.parsing import lightning_getattr, lightning_hasattr, lightning_setattr
from pytorch_lightning.utilities.rank_zero import rank_zero_info, rank_zero_warn

if TYPE_CHECKING:
    from pytorch_lightning.loggers.base import LightningLoggerBase

    class _BatchSizeFinderDumpedParams(TypedDict):
        callbacks: List[Callback]
        logger: Optional[LightningLoggerBase]
        max_steps: int
        limit_val_batches: Union[int, float]
        limit_eval_batches: Union[int, float]


log = logging.getLogger(__name__)


class BatchSizeFinder(Callback):
    SUPPORTED_MODES = ("power", "binsearch")

    def __init__(
        self,
        mode: str = "power",
        steps_per_trial: int = 3,
        init_val: int = 2,
        max_trials: int = 25,
        batch_arg_name: str = "batch_size",
    ) -> None:
        """The `BatchSizeFinder` callback tries to find the largest batch size for a given model that does not give
        an out of memory (OOM) error. It works with both training and evalation. All you need to do is add it as a
        callback inside Trainer and call ``trainer.fit/validate/test/predict()``. Internally it calls the
        respective step function ``steps_per_trial`` times for each batch size until one of the batch size
        generates and OOM error.

        Args:
            mode: search strategy to update the batch size:

                - ``'power'``: Keep multiplying the batch size by 2, until we get an OOM error.
                - ``'binsearch'``: Initially keep multiplying by 2 and after encountering an OOM error
                    do a binary search between the last successful batch size and the batch size that failed.

            steps_per_trial: number of steps to run with a given batch size.
                Ideally 1 should be enough to test if a OOM error occurs,
                however in practice a few are needed.

            init_val: initial batch size to start the search with.

            max_trials: max number of increase in batch size done before
               algorithm is terminated

            batch_arg_name: name of the attribute that stores the batch size.
                It is expected that the user has provided a model or datamodule that has a hyperparameter
                with that name. We will look for this attribute name in the following places

                - ``model``
                - ``model.hparams``
                - ``trainer.datamodule`` (the datamodule passed to the tune method)
        """
        # TODO: Add input validation.
        mode = mode.lower()
        if mode not in self.SUPPORTED_MODES:
            raise MisconfigurationException(f"`mode` should be either of {self.SUPPORTED_MODES}")

        self.mode = mode
        self.steps_per_trial = steps_per_trial
        self.init_val = init_val
        self.max_trials = max_trials
        self.batch_arg_name = batch_arg_name
        self.optimal_batch_size = init_val

        self._early_exit = False
        self._dumped_params: _BatchSizeFinderDumpedParams = {}

    def setup(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule", stage: Optional[str] = None) -> None:
        if trainer._accelerator_connector.is_distributed:
            raise MisconfigurationException("Batch size finder is not supported with distributed strategies.")

        running_stage = trainer.state.stage
        dl_source = getattr(trainer._data_connector, f"_{running_stage.dataloader_prefix}_dataloader_source")

        # TODO: check if this can be enabled (#4040)
        if not trainer._data_connector._train_dataloader_source.is_module():
            raise MisconfigurationException(
                "Batch size finder cannot be used with dataloaders passed directly to `.fit()`. Please disable"
                " the feature or incorporate the dataloader into your LightningModule or LightningDataModule."
            )

        # TODO: Add support for multiple eval dataloader
        if stage != "fit":
            dataloaders = dl_source.dataloader()
            if isinstance(dataloaders, list) and len(dataloaders) > 1:
                raise MisconfigurationException(
                    "Batch size finder cannot be used with multiple" f" {running_stage.dataloader_prefix} dataloaders."
                )

        if not lightning_hasattr(pl_module, self.batch_arg_name):
            raise MisconfigurationException(
                f"Field {self.batch_arg_name} not found in both `model` and `model.hparams`"
            )

    def scale_batch_size(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        if trainer.fast_dev_run:
            rank_zero_warn("Skipping batch size scaler since `fast_dev_run` is enabled.")
            return

        if (
            hasattr(pl_module, self.batch_arg_name)
            and hasattr(pl_module, "hparams")
            and self.batch_arg_name in pl_module.hparams
        ):
            rank_zero_warn(
                f"Field `model.{self.batch_arg_name}` and `model.hparams.{self.batch_arg_name}` are mutually exclusive!"
                f" `model.{self.batch_arg_name}` will be used as the initial batch size for scaling."
                " If this is not the intended behavior, please remove either one."
            )

        # Save initial model, that is loaded after batch size is found
        ckpt_path = os.path.join(trainer.default_root_dir, f".scale_batch_size_{uuid.uuid4()}.ckpt")
        trainer.save_checkpoint(ckpt_path)

        # Arguments we adjust during the batch size finder, save for restoring
        self._dump_params(trainer)

        # Set to values that are required by the algorithm
        self._reset_params(trainer)

        if trainer.progress_bar_callback:
            trainer.progress_bar_callback.disable()

        new_size, _ = self._adjust_batch_size(trainer, value=self.init_val)

        if self.mode == "power":
            new_size = self._run_power_scaling(trainer, pl_module, new_size)
        elif self.mode == "binsearch":
            new_size = self._run_binary_scaling(trainer, pl_module, new_size)

        _collect_garbage(trainer)

        log.info(f"Finished batch size finder, will continue with full run using batch size {new_size}")

        self._restore_params(trainer)

        if trainer.progress_bar_callback:
            trainer.progress_bar_callback.enable()

        trainer._checkpoint_connector.restore(ckpt_path)
        trainer.strategy.remove_checkpoint(ckpt_path)

        self.optimal_batch_size = new_size

        if self._early_exit:
            raise _TunerExitException()

    def _run_power_scaling(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule", new_size: int) -> int:
        """Batch scaling mode where the size is doubled at each iteration until an OOM error is encountered."""
        for _ in range(self.max_trials):
            _collect_garbage(trainer)

            try:
                self._try_loop_run(trainer)
                new_size, changed = self._adjust_batch_size(trainer, factor=2.0, desc="succeeded")

                if changed:
                    # Force the dataloaders to reset as the batch size has changed
                    self._reset_dataloaders(trainer, pl_module)
                else:
                    break
            except RuntimeError as exception:
                if is_oom_error(exception):
                    _collect_garbage(trainer)

                    new_size, _ = self._adjust_batch_size(trainer)
                    break
                else:
                    raise  # some other error not memory related

        return new_size

    def _run_binary_scaling(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule", new_size: int) -> int:
        """Batch scaling mode where the size is initially is doubled at each iteration until an OOM error is
        encountered.

        Hereafter, the batch size is further refined using a binary search
        """
        low = 1
        high = None
        count = 0
        while True:
            _collect_garbage(trainer)

            try:
                # run loop
                self._try_loop_run(trainer)
                count += 1
                if count > self.max_trials:
                    break
                # Double in size
                low = new_size
                if high:
                    if high - low <= 1:
                        break
                    midval = (high + low) // 2
                    new_size, changed = self._adjust_batch_size(trainer, value=midval, desc="succeeded")
                else:
                    new_size, changed = self._adjust_batch_size(trainer, factor=2.0, desc="succeeded")

                if changed:
                    # Force the dataloaders to reset as the batch size has changed
                    self._reset_dataloaders(trainer, pl_module)
                else:
                    break

            except RuntimeError as exception:
                # Only these errors should trigger an adjustment
                if is_oom_error(exception):
                    # If we fail in power mode, half the size and return
                    _collect_garbage(trainer)

                    high = new_size
                    midval = (high + low) // 2
                    new_size, changed = self._adjust_batch_size(trainer, value=midval, desc="failed")

                    if changed:
                        # Force the dataloaders to reset as the batch size has changed
                        self._reset_dataloaders(trainer, pl_module)

                    if high - low <= 1:
                        break
                else:
                    raise  # some other error not memory related

        return new_size

    def _try_loop_run(self, trainer: "pl.Trainer") -> None:
        if trainer.state.fn == "fit":
            loop = trainer.fit_loop
        else:
            loop = getattr(trainer, f"{trainer.state.stage}_loop")

        loop.load_state_dict(deepcopy(self._dumped_params["loop_state_dict"]))
        loop.restarting = False
        loop.run()

    @staticmethod
    def _reset_dataloaders(trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        if trainer.state.fn == "fit":
            trainer.reset_train_dataloader(pl_module)
            trainer.reset_val_dataloader(pl_module)
        else:
            stage = trainer.state.stage
            getattr(trainer, f"reset_{stage.dataloader_prefix}_dataloader")(pl_module)

    def _dump_params(self, trainer: "pl.Trainer") -> None:

        self._dumped_params = {
            "logger": trainer.logger,
            "callbacks": trainer.callbacks,
        }

        if trainer.state.fn == "fit":
            loop = trainer.fit_loop
            self._dumped_params["max_steps"] = trainer.max_steps
            self._dumped_params["limit_val_batches"] = trainer.limit_val_batches
        else:
            stage = trainer.state.stage
            loop = getattr(trainer, f"{stage}_loop")
            self._dumped_params["limit_eval_batches"] = getattr(trainer, f"limit_{stage.dataloader_prefix}_batches")

            if hasattr(loop, "verbose"):
                self._dumped_params["loop_verbose"] = loop.verbose

        self._dumped_params["loop_state_dict"] = deepcopy(loop.state_dict())

    def _reset_params(self, trainer: "pl.Trainer") -> None:
        from pytorch_lightning.loggers.logger import DummyLogger

        trainer.logger = DummyLogger() if trainer.logger is not None else None
        trainer.callbacks = []

        if trainer.state.fn == "fit":
            trainer.limit_val_batches = self.steps_per_trial
            trainer.fit_loop.max_steps = self.steps_per_trial
        else:
            stage = trainer.state.stage
            loop = getattr(trainer, f"{stage}_loop")
            setattr(trainer, f"limit_{stage.dataloader_prefix}_batches", self.steps_per_trial)

            if hasattr(loop, "verbose"):
                loop.verbose = False

    def _restore_params(self, trainer: "pl.Trainer") -> None:
        # TODO: There are more states that needs to be reset (#4512 and #4870)
        trainer.logger = self._dumped_params["logger"]
        trainer.callbacks = self._dumped_params["callbacks"]

        if trainer.state.fn == "fit":
            loop = trainer.fit_loop
            loop.max_steps = self._dumped_params["max_steps"]
            trainer.limit_val_batches = self._dumped_params["limit_val_batches"]
        else:
            stage = trainer.state.stage
            loop = getattr(trainer, f"{stage}_loop")
            setattr(trainer, f"limit_{stage.dataloader_prefix}_batches", self._dumped_params["limit_eval_batches"])

        loop.load_state_dict(deepcopy(self._dumped_params["loop_state_dict"]))
        loop.restarting = False
        if "loop_verbose" in self._dumped_params:
            loop.verbose = self._dumped_params["loop_verbose"]

    def on_fit_start(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        self.scale_batch_size(trainer, pl_module)

    def on_validation_start(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        if trainer.sanity_checking or trainer.state.fn != "validate":
            return

        self.scale_batch_size(trainer, pl_module)

    def on_test_start(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        self.scale_batch_size(trainer, pl_module)

    def on_predict_start(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule") -> None:
        self.scale_batch_size(trainer, pl_module)

    def _adjust_batch_size(
        self,
        trainer: "pl.Trainer",
        factor: float = 1.0,
        value: Optional[int] = None,
        desc: Optional[str] = None,
    ) -> Tuple[int, bool]:
        """Helper function for adjusting the batch size.

        Args:
            trainer: instance of pytorch_lightning.Trainer
            factor: value which the old batch size is multiplied by to get the
                new batch size
            value: if a value is given, will override the batch size with this value.
                Note that the value of `factor` will not have an effect in this case
            desc: either ``"succeeded"`` or ``"failed"``. Used purely for logging

        Returns:
            The new batch size for the next trial and a bool that signals whether the
            new value is different than the previous batch size.
        """
        model = trainer.lightning_module
        batch_size = lightning_getattr(model, self.batch_arg_name)
        new_size = value if value is not None else int(batch_size * factor)
        if desc:
            rank_zero_info(f"Batch size {batch_size} {desc}, trying batch size {new_size}")

        # TODO improve this for multi eval dataloaders
        if trainer.state.fn == "fit":
            if not self._is_valid_batch_size(new_size, trainer.train_dataloader, trainer):
                new_size = min(new_size, len(trainer.train_dataloader.dataset))
        else:
            stage = trainer.state.stage
            dataloaders = getattr(trainer, f"{stage.dataloader_prefix}_dataloaders")
            if not self._is_valid_batch_size(new_size, dataloaders[0], trainer):
                new_size = min(new_size, len(dataloaders[0].dataset))

        changed = new_size != batch_size
        lightning_setattr(model, self.batch_arg_name, new_size)
        return new_size, changed

    @staticmethod
    def _is_valid_batch_size(batch_size: int, dataloader: DataLoader, trainer: "pl.Trainer") -> bool:
        from pytorch_lightning.utilities.data import has_len_all_ranks

        module = trainer.lightning_module or trainer.datamodule
        return not has_len_all_ranks(dataloader, trainer.strategy, module) or batch_size <= len(dataloader)


def _collect_garbage(trainer: "pl.Trainer"):
    from pytorch_lightning.accelerators.gpu import GPUAccelerator

    if isinstance(trainer.accelerator, GPUAccelerator):
        garbage_collection_cuda()
