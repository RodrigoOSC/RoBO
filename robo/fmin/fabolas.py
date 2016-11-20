import time
import george
import logging
import numpy as np

from robo.models.gaussian_process_mcmc import FabolasGP
from robo.initial_design import init_random_uniform
from robo.priors.env_priors import EnvPrior
from robo.acquisition_functions.information_gain_per_unit_cost import InformationGainPerUnitCost
from robo.acquisition_functions.marginalization import MarginalizationGPMCMC
from robo.maximizers.direct import Direct
from robo.util.incumbent_estimation import projected_incumbent_estimation


logger = logging.getLogger(__name__)


def transform(s, s_min, s_max):
    s_transform = (np.log(s) - np.log(s_min)) / (np.log(s_max) - np.log(s_min))
    return s_transform


def retransform(s_transform, s_min, s_max):
    s = np.rint(np.exp(s_transform * (np.log(s_max) - np.log(s_min)) + np.log(s_min)))
    return s


def fabolas(objective_function, lower, upper, s_min, s_max,
         n_init=2, num_iterations=30,
         burnin=100, chain_length=200, rng=None):
    """
    Fast Bayesian Optimization of Machine Learning Hyperparameters
    on Large Datasets


    Parameters
    ----------
    objective_function: function
        Objective function that will be optimized
    lower: np.array(D,)
        Lower bound of the input space
    upper: np.array(D,)
        Upper bound of the input space
    n_tasks: int
        Number of task
    n_init: int
        Number of initial design points
    num_iterations: int
        Number of iterations
    chain_length : int
        The length of the MCMC chain for each walker.
    burnin : int
        The number of burnin steps before the actual MCMC sampling starts.
    rng: numpy.random.RandomState
        Random number generator

    Returns
    -------
        dict with all results
    """

    assert n_init <= num_iterations, "Number of initial design point has to be <= than the number of iterations"
    assert lower.shape[0] == upper.shape[0], "Dimension miss match between upper and lower bound"

    time_start = time.time()
    if rng is None:
        rng = np.random.RandomState(np.random.randint(0, 10000))

    n_dims = lower.shape[0]

    # Bookkeeping
    time_func_eval = []
    time_overhead = []
    incumbents = []
    runtime = []

    X = []
    y = []
    c = []

    # Define model for the objective function
    cov_amp = 1  # Covariance amplitude
    kernel = cov_amp

    # ARD Kernel for the configuration space
    for d in range(n_dims):
        kernel *= george.kernels.Matern52Kernel(np.ones([1]) * 0.01,
                                                ndim=n_dims+1, dim=d)

    # Kernel for the environmental variable
    # We use (1-s)**2 as basis function for the Bayesian linear kernel
    degree = 1
    env_kernel = george.kernels.BayesianLinearRegressionKernel(n_dims+1,
                                                               dim=n_dims,
                                                               degree=degree)
    env_kernel[:] = np.ones([degree + 1]) * 0.1

    # Take 3 times more samples than we have hyperparameters
    n_hypers = 3 * len(kernel)
    if n_hypers % 2 == 1:
        n_hypers += 1

    prior = EnvPrior(len(kernel) + 1,
                     n_ls=n_dims,
                     n_lr=(degree + 1),
                     rng=rng)

    linear_bf = lambda x: x
    quadratic_bf = lambda x: (1 - x) ** 2

    model_objective = FabolasGP(kernel,
                                prior=prior,
                                burnin_steps=burnin,
                                chain_length=chain_length,
                                basis_func=quadratic_bf,
                                n_hypers=n_hypers,
                                normalize_input=False,
                                lower=lower,
                                upper=upper,
                                rng=rng)

    # Define model for the cost function
    cost_cov_amp = 1

    cost_kernel = cost_cov_amp

    # ARD Kernel for the configuration space
    for d in range(n_dims):
        cost_kernel *= george.kernels.Matern52Kernel(np.ones([1]) * 0.01,
                                                     ndim=n_dims+1, dim=d)

    cost_degree = 1
    cost_env_kernel = george.kernels.BayesianLinearRegressionKernel(
                                                            n_dims+1,
                                                            dim=n_dims,
                                                            degree=cost_degree)
    cost_env_kernel[:] = np.ones([cost_degree + 1]) * 0.1

    cost_kernel *= cost_env_kernel

    cost_prior = EnvPrior(len(cost_kernel) + 1,
                          n_ls=n_dims,
                          n_lr=(cost_degree + 1),
                          rng=rng)

    model_cost = FabolasGP(cost_kernel,
                           prior=cost_prior,
                           basis_func=linear_bf,
                           burnin_steps=burnin,
                           chain_length=chain_length,
                           n_hypers=n_hypers,
                           normalize_input=False,
                           lower=lower,
                           upper=upper,
                           rng=rng)

    # Extend input space by task variable
    extend_lower = np.append(lower, 0)
    extend_upper = np.append(upper, 1)
    is_env = np.zeros(extend_lower.shape[0])
    is_env[-1] = 1

    # Define acquisition function and maximizer
    ig = InformationGainPerUnitCost(model_objective,
                                    model_cost,
                                    extend_lower,
                                    extend_upper,
                                    is_env_variable=is_env,
                                    n_representer=10)
    acquisition_func = MarginalizationGPMCMC(ig)
    maximizer = Direct(acquisition_func, extend_lower, extend_upper, verbose=False)

    subsets = [256, 128, 64, 32]
    # Initial Design
    for i in range(n_init):
        start_time_overhead = time.time()
        # Draw random configuration
        s = int(s_max / float(subsets[i % len(subsets)]))
        x = init_random_uniform(lower, upper, 1, rng)[0]
        st = time.time()
        func_val, cost = objective_function(x, s)
        time_func_eval.append(time.time() - st)

        # Bookkeeping
        s_transformed = transform(s)
        config = np.append(x, s_transformed)
        X.append(config)
        y.append(func_val)
        c.append(cost)

        # Estimate incumbent as the best observed value so far
        best_idx = np.argmin(y)
        incumbents.append(np.append(X[best_idx], s_max))  # Incumbent is always on s=s_max

        time_overhead.append(time.time() - start_time_overhead)
        runtime.append(time.time() - time_start)

    X = np.array(X)
    y = np.array(y)
    c = np.array(c)

    for it in range(n_init, num_iterations):
        logger.info("Start iteration %d ... ", it)

        start_time = time.time()

        # Train models
        model_objective.train(X, y, do_optimize=True)
        model_cost.train(X, c, do_optimize=True)

        # Estimate incumbent by projecting all observed points to the task of interest and
        # pick the point with the lowest mean prediction
        incumbent, incumbent_value = projected_incumbent_estimation(model_objective, X[:, :-1],
                                                                    proj_value=s_max)
        incumbents.append(incumbent)
        logger.info("Current incumbent %s with estimated performance %f" % (str(incumbent), incumbent_value))

        # Maximize acquisition function
        acquisition_func.update(model_objective, model_cost)
        new_x = maximizer.maximize()
        s = retransform(new_x[-1], s_min, s_max)  # Map s from log space to original linear space

        time_overhead.append(time.time() - start_time)
        logger.info("Optimization overhead was %f seconds" % time_overhead[-1])

        # Evaluate the chosen configuration
        logger.info("Evaluate candidate %s" % (str(new_x)))
        start_time = time.time()
        new_y, new_c = objective_function(new_x[:-1], s)
        time_func_eval.append(time.time() - start_time)

        logger.info("Configuration achieved a performance of %f with cost %f" % (new_y, new_c))
        logger.info("Evaluation of this configuration took %f seconds" % time_func_eval[-1])

        # Add new observation to the data
        X = np.concatenate((X, new_x[None, :]), axis=0)
        y = np.concatenate((y, np.array([new_y])), axis=0)
        c = np.concatenate((c, np.array([new_c])), axis=0)

        runtime.append(time.time() - time_start)

    # Estimate the final incumbent
    model_objective.train(X, y)
    incumbent, incumbent_value = projected_incumbent_estimation(model_objective, X[:, :-1],
                                                                proj_value=1)
    incumbent[-1] = s_max
    incumbents.append(incumbent)
    logger.info("Final incumbent %s with estimated performance %f" % (str(incumbent), incumbent_value))

    results = dict()
    results["x_opt"] = incumbent[:-1]
    results["trajectory"] = [inc for inc in incumbents]
    results["runtime"] = runtime
    results["overhead"] = time_overhead
    results["time_func_eval"] = time_func_eval
    return results

