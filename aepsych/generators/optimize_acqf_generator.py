#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
import time
from typing import Any, Dict, Optional

import numpy as np
import torch
from aepsych.config import Config
from aepsych.generators.base import AEPsychGenerator
from aepsych.models.base import ModelProtocol
from aepsych.utils import make_scaled_sobol
from aepsych.utils_logging import getLogger
from botorch.acquisition import AcquisitionFunction
from botorch.optim import optimize_acqf

logger = getLogger()


class OptimizeAcqfGenerator(AEPsychGenerator):
    """Generator that chooses points by minimizing an acquisition function."""

    def __init__(
        self,
        acqf: AcquisitionFunction,
        acqf_kwargs: Optional[Dict[str, Any]] = None,
        restarts: int = 10,
        samps: int = 1000,
        max_gen_time: Optional[float] = None,
    ) -> None:
        """Initialize OptimizeAcqfGenerator.
        Args:
            acqf (AcquisitionFunction): Acquisition function to use.
            acqf_kwargs (Dict[str, object], optional): Extra arguments to
                pass to acquisition function. Defaults to no arguments.
            restarts (int): Number of restarts for acquisition function optimization.
            samps (int): Number of samples for quasi-random initialization of the acquisition function optimizer.
            max_gen_time (optional, float): Maximum time (in seconds) to optimize the acquisition function.
                This is only loosely followed by scipy's optimizer, so consider using a number about 1/3 or
                less of what your true upper bound is.
        """

        if acqf_kwargs is None:
            acqf_kwargs = {}
        self.acqf = acqf
        self.acqf_kwargs = acqf_kwargs
        self.restarts = restarts
        self.samps = samps
        self.max_gen_time = max_gen_time

    def _instantiate_acquisition_fn(self, model: ModelProtocol, train_x):
        if self.acqf in self.baseline_requiring_acqfs:
            return self.acqf(model=model, X_baseline=train_x, **self.acqf_kwargs)
        else:
            return self.acqf(model=model, **self.acqf_kwargs)

    def gen(self, num_points: int, model: ModelProtocol) -> np.ndarray:
        """Query next point(s) to run by optimizing the acquisition function.
        Args:
            num_points (int, optional): Number of points to query.
            model (ModelProtocol): Fitted model of the data.
        Returns:
            np.ndarray: Next set of point(s) to evaluate, [num_points x dim].
        """

        # eval should be inherited from superclass
        model.eval()  # type: ignore
        train_x = model.train_inputs[0]
        acqf = self._instantiate_acquisition_fn(model, train_x)

        logger.info("Starting gen...")
        starttime = time.time()

        if self.max_gen_time is None:
            new_candidate, _ = optimize_acqf(
                acq_function=acqf,
                bounds=torch.tensor(np.c_[model.lb, model.ub]).T.to(train_x),
                q=num_points,
                num_restarts=self.restarts,
                raw_samples=self.samps,
            )
        else:
            # figure out how long evaluating a single samp
            starttime = time.time()
            _ = acqf(train_x[0:1, :])
            single_eval_time = time.time() - starttime

            # only a heuristic for total num evals since everything is stochastic,
            # but the reasoning is: we initialize with self.samps samps, subsample
            # self.restarts from them in proportion to the value of the acqf, and
            # run that many optimization. So:
            # total_time = single_eval_time * n_eval * restarts + single_eval_time * samps
            # and we solve for n_eval
            n_eval = int(
                (self.max_gen_time - single_eval_time * self.samps)
                / (single_eval_time * self.restarts)
            )
            if n_eval > 10:
                # heuristic, if we can't afford 10 evals per restart, just use quasi-random search
                options = {"maxfun": n_eval}
                logger.info(f"gen maxfun is {n_eval}")

                new_candidate, _ = optimize_acqf(
                    acq_function=acqf,
                    bounds=torch.tensor(np.c_[model.lb, model.ub]).T.to(train_x),
                    q=num_points,
                    num_restarts=self.restarts,
                    raw_samples=self.samps,
                    options=options,
                )
            else:
                logger.info(f"gen maxfun is {n_eval}, falling back to random search...")
                nsamp = max(int(self.max_gen_time / single_eval_time), 10)
                # Generate the points at which to sample
                X = make_scaled_sobol(lb=model.lb, ub=model.ub, size=nsamp)

                acqvals = acqf(X[:, None, :])

                best_indx = torch.argmax(acqvals, dim=0)
                new_candidate = X[best_indx, None]

        logger.info(f"Gen done, time={time.time()-starttime}")
        return new_candidate.numpy()

    @classmethod
    def from_config(cls, config: Config):
        classname = cls.__name__
        acqf = config.getobj(classname, "acqf", fallback=None)
        extra_acqf_args = cls._get_acqf_options(acqf, config)

        restarts = config.getint(classname, "restarts", fallback=10)
        samps = config.getint(classname, "samps", fallback=1000)

        max_gen_time = config.getfloat(classname, "max_gen_time", fallback=None)

        return cls(
            acqf=acqf,
            acqf_kwargs=extra_acqf_args,
            restarts=restarts,
            samps=samps,
            max_gen_time=max_gen_time,
        )
