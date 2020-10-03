import argparse
import itertools
import os
import time

import numpy as onp

import jax
from jax import vmap
import jax.numpy as np
import jax.random as random

import numpyro
import numpyro.distributions as dist
from numpyro.infer import MCMC, NUTS
import matplotlib.pyplot as plt
import corner

# setup hyperparameters
# hypers = {'expected_sparsity': max(1.0, X.shape[1]/2),
#           'alpha1': alpha1, 'beta1': beta1,
#           'alpha2': alpha2, 'beta2': beta2,
#           'alpha3': alpha3, 'c': c,
#           'alpha_obs': alpha_obs, 'beta_obs': beta_obs}

def dot(X, Z):
    return np.dot(X, Z[..., None])[..., 0]

class SKIM():
    def __init__(X, Y, hypers=None, ):
        self.X = X
        self.Y = Y
        self.hypers = hypers

    # The kernel that corresponds to our quadratic regressor. (According to prop 6.1)
    def kernel(X, Z, eta1, eta2, c, jitter=1.0e-6):
        eta1sq = np.square(eta1)
        eta2sq = np.square(eta2)
        k1 = 0.5 * eta2sq * np.square(1.0 + dot(X, Z))
        k2 = -0.5 * eta2sq * dot(np.square(X), np.square(Z))
        k3 = (eta1sq - eta2sq) * dot(X, Z)
        k4 = np.square(c) - 0.5 * eta2sq
        if X.shape == Z.shape:
            k4 += jitter * np.eye(X.shape[0])
        return k1 + k2 + k3 + k4


    # Most of the model code is concerned with constructing the sparsity inducing prior.
    def model(X, Y, hypers):
        # Here X is the design matrix with N x p dimensions
        # read off dimensions P and N
        # S -  sparsity coeff
        S, P, N = hypers['expected_sparsity'], X.shape[1], X.shape[0]

        # sample variables from p. 18
        sigma = numpyro.sample("sigma", dist.HalfNormal(hypers['alpha3']))
        phi = sigma * (S / np.sqrt(N)) / (P - S)
        eta1 = numpyro.sample("eta1", dist.HalfCauchy(phi))

        msq = numpyro.sample("msq", dist.InverseGamma(hypers['alpha1'], hypers['beta1']))
        xisq = numpyro.sample("xisq", dist.InverseGamma(hypers['alpha2'], hypers['beta2']))

        eta2 = np.square(eta1) * np.sqrt(xisq) / msq

        lam = numpyro.sample("lambda", dist.HalfCauchy(np.ones(P)))
        kappa = np.sqrt(msq) * lam / np.sqrt(msq + np.square(eta1 * lam))

        # sample observation noise
        var_obs = numpyro.sample("var_obs", dist.InverseGamma(hypers['alpha_obs'], hypers['beta_obs']))

        # compute kernel (as in proposition 6.1)
        kX = kappa * X
        k = kernel(kX, kX, eta1, eta2, hypers['c']) + var_obs * np.eye(N)
        assert k.shape == (N, N)

        # sample Y according to the standard gaussian process formula
        numpyro.sample("Y", dist.MultivariateNormal(loc=np.zeros(X.shape[0]), covariance_matrix=k),
                      obs=Y)

        
        

    # Compute the mean and variance of coefficient theta_i (where i = dimension) for a
    # MCMC sample of the kernel hyperparameters (eta1, xisq, ...).
    # Compare to theorem 5.1 in reference [1].
    def compute_singleton_mean_variance(X, Y, dimension, msq, lam, eta1, xisq, c, var_obs):
        P, N = X.shape[1], X.shape[0]

        probe = np.zeros((2, P))
        probe = jax.ops.index_update(probe, jax.ops.index[:, dimension], np.array([1.0, -1.0]))

        eta2 = np.square(eta1) * np.sqrt(xisq) / msq
        kappa = np.sqrt(msq) * lam / np.sqrt(msq + np.square(eta1 * lam))

        kX = kappa * X
        kprobe = kappa * probe

        k_xx = kernel(kX, kX, eta1, eta2, c) + var_obs * np.eye(N)
        k_xx_inv = np.linalg.inv(k_xx)
        k_probeX = kernel(kprobe, kX, eta1, eta2, c)
        k_prbprb = kernel(kprobe, kprobe, eta1, eta2, c)

        vec = np.array([0.50, -0.50]) ## a = (1/2, -1/2)
        mu = np.matmul(k_probeX, np.matmul(k_xx_inv, Y))
        mu = np.dot(mu, vec)

        var = k_prbprb - np.matmul(k_probeX, np.matmul(k_xx_inv, np.transpose(k_probeX)))
        var = np.matmul(var, vec)
        var = np.dot(var, vec)

        return mu, var


    # Compute the mean and variance of coefficient theta_ij for a MCMC sample of the
    # kernel hyperparameters (eta1, xisq, ...). Compare to theorem 5.1 in reference [1].
    def compute_pairwise_mean_variance(X, Y, dim1, dim2, msq, lam, eta1, xisq, c, var_obs):
        # Here X is the design matrix with N x p dimensions
        # read off dimensions P and N
        P, N = X.shape[1], X.shape[0]

        probe = np.zeros((4, P))
        probe = jax.ops.index_update(probe, jax.ops.index[:, dim1], np.array([1.0, 1.0, -1.0, -1.0]))
        probe = jax.ops.index_update(probe, jax.ops.index[:, dim2], np.array([1.0, -1.0, 1.0, -1.0]))
        
        
        # compute eta2 and kappa from p. 18 

        eta2 = np.square(eta1) * np.sqrt(xisq) / msq
        kappa = np.sqrt(msq) * lam / np.sqrt(msq + np.square(eta1 * lam))

        kX = kappa * X
        kprobe = kappa * probe

        # ?? compute a bunch of matrices w/ kernels ??
        k_xx = kernel(kX, kX, eta1, eta2, c) + var_obs * np.eye(N)
        k_xx_inv = np.linalg.inv(k_xx)
        k_probeX = kernel(kprobe, kX, eta1, eta2, c)
        k_prbprb = kernel(kprobe, kprobe, eta1, eta2, c)

        vec = np.array([0.25, -0.25, -0.25, 0.25]) ## ?? not sure why not (-1/2, 1/2, -1, 1) ??
        mu = np.matmul(k_probeX, np.matmul(k_xx_inv, Y))
        mu = np.dot(mu, vec)

        var = k_prbprb - np.matmul(k_probeX, np.matmul(k_xx_inv, np.transpose(k_probeX)))
        var = np.matmul(var, vec)
        var = np.dot(var, vec)

        return mu, var


    # Sample coefficients theta from the posterior for a given MCMC sample.
    # The first P returned values are {theta_1, theta_2, ...., theta_P}, while
    # the remaining values are {theta_ij} for i,j in the list `active_dims`,
    # sorted so that i < j.
    def sample_theta_space(X, Y, active_dims, msq, lam, eta1, xisq, c, var_obs): #(section B.5) ?
        # Here X is the design matrix with N x p dimensions
        # read off dimensions P and N
        # and number of active dimensions
        P, N, M = X.shape[1], X.shape[0], len(active_dims)
        
        # the total number of coefficients we return
        num_coefficients = P + M * (M - 1) // 2

        probe = np.zeros((2 * P + 2 * M * (M - 1), P))
        vec = np.zeros((num_coefficients, 2 * P + 2 * M * (M - 1)))
        start1 = 0
        start2 = 0

        for dim in range(P):
            probe = jax.ops.index_update(probe, jax.ops.index[start1:start1 + 2, dim], np.array([1.0, -1.0]))
            vec = jax.ops.index_update(vec, jax.ops.index[start2, start1:start1 + 2], np.array([0.5, -0.5]))
            start1 += 2
            start2 += 1

        for dim1 in active_dims:
            for dim2 in active_dims:
                if dim1 >= dim2:
                    continue
                probe = jax.ops.index_update(probe, jax.ops.index[start1:start1 + 4, dim1],
                                            np.array([1.0, 1.0, -1.0, -1.0]))
                probe = jax.ops.index_update(probe, jax.ops.index[start1:start1 + 4, dim2],
                                            np.array([1.0, -1.0, 1.0, -1.0]))
                vec = jax.ops.index_update(vec, jax.ops.index[start2, start1:start1 + 4],
                                          np.array([0.25, -0.25, -0.25, 0.25]))
                start1 += 4
                start2 += 1

        eta2 = np.square(eta1) * np.sqrt(xisq) / msq
        kappa = np.sqrt(msq) * lam / np.sqrt(msq + np.square(eta1 * lam))

        kX = kappa * X
        kprobe = kappa * probe

        k_xx = kernel(kX, kX, eta1, eta2, c) + var_obs * np.eye(N)
        k_xx_inv = np.linalg.inv(k_xx)
        k_probeX = kernel(kprobe, kX, eta1, eta2, c)
        k_prbprb = kernel(kprobe, kprobe, eta1, eta2, c)

        mu = np.matmul(k_probeX, np.matmul(k_xx_inv, Y))
        mu = np.sum(mu * vec, axis=-1)

        covar = k_prbprb - np.matmul(k_probeX, np.matmul(k_xx_inv, np.transpose(k_probeX)))
        covar = np.matmul(vec, np.matmul(covar, np.transpose(vec)))
        L = np.linalg.cholesky(covar)

        # sample from N(mu, covar)
        sample = mu + np.matmul(L, onp.random.randn(num_coefficients))

        return sample

    # MODIFICATION to original method to return flat posterior samples from the
    # MCMC, but only for active dimensions
    def sample_theta_posterior(X, Y, active_dims, msq, lam, eta1, xisq, c, var_obs, N_samps, dim_pair_arr): 
        P, N, M = X.shape[1], X.shape[0], len(active_dims)
        
        num_coefficients = P + M * (M - 1) // 2

        probe = np.zeros((2 * P + 2 * M * (M - 1), P))
        vec = np.zeros((num_coefficients, 2 * P + 2 * M * (M - 1)))
        start1 = 0
        start2 = 0

        for dim in range(P):
            probe = jax.ops.index_update(probe, jax.ops.index[start1:start1 + 2, dim], np.array([1.0, -1.0]))
            vec = jax.ops.index_update(vec, jax.ops.index[start2, start1:start1 + 2], np.array([0.5, -0.5]))
            start1 += 2
            start2 += 1

        for dim1 in active_dims:
            for dim2 in active_dims:
                if dim1 >= dim2:
                    continue
                probe = jax.ops.index_update(probe, jax.ops.index[start1:start1 + 4, dim1],
                                            np.array([1.0, 1.0, -1.0, -1.0]))
                probe = jax.ops.index_update(probe, jax.ops.index[start1:start1 + 4, dim2],
                                            np.array([1.0, -1.0, 1.0, -1.0]))
                vec = jax.ops.index_update(vec, jax.ops.index[start2, start1:start1 + 4],
                                          np.array([0.25, -0.25, -0.25, 0.25]))
                start1 += 4
                start2 += 1

        eta2 = np.square(eta1) * np.sqrt(xisq) / msq
        kappa = np.sqrt(msq) * lam / np.sqrt(msq + np.square(eta1 * lam))

        kX = kappa * X
        kprobe = kappa * probe

        k_xx = kernel(kX, kX, eta1, eta2, c) + var_obs * np.eye(N)
        k_xx_inv = np.linalg.inv(k_xx)
        k_probeX = kernel(kprobe, kX, eta1, eta2, c)
        k_prbprb = kernel(kprobe, kprobe, eta1, eta2, c)

        mu = np.matmul(k_probeX, np.matmul(k_xx_inv, Y))
        mu = np.sum(mu * vec, axis=-1)

        covar = k_prbprb - np.matmul(k_probeX, np.matmul(k_xx_inv, np.transpose(k_probeX)))
        covar = np.matmul(vec, np.matmul(covar, np.transpose(vec)))
        L = np.linalg.cholesky(covar)

        # sample from N(mu, covar)
        sample = mu + np.matmul(L, onp.random.randn(num_coefficients))
        
        ####### ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##########
        ####### ~~~~~~~~~~~~~ CHANGES to the original method ~~~~~~~~~~~~~~~~~~~ #########
        
        # include active direct and pairwise interactions terms only 
        all_active_dims = active_dims + dim_pair_arr
        mu_active = np.array([mu[i] for i in all_active_dims])
        
        cov_active = []
        for j in all_active_dims:
            cov_act_j = [covar[j][i] for i in all_active_dims]
            cov_active.append(cov_act_j)
        cov_active = onp.array(cov_active)
        
        # return posterior samples
        rng_key = random.PRNGKey(0)
        samps = numpyro.distributions.MultivariateNormal(loc = onp.array(mu_active), 
                                                        covariance_matrix = cov_active).sample(rng_key, sample_shape = (1, N_samps))

        samps_new = onp.reshape(samps, (N_samps, len(all_active_dims)))
        
        return samps_new


    # Helper function for doing HMC inference
    def run_inference(model, args, rng_key, X, Y, hypers):
        start = time.time()
        kernel = NUTS(model)
        mcmc = MCMC(kernel, args.num_warmup, args.num_samples, num_chains=args.num_chains,
                    progress_bar=False if "NUMPYRO_SPHINXBUILD" in os.environ else True)
        mcmc.run(rng_key, X, Y, hypers)
        mcmc.print_summary()
        print('\nMCMC elapsed time:', time.time() - start)
        return mcmc.get_samples()


    # Get the mean and variance of a gaussian mixture
    def gaussian_mixture_stats(mus, variances):
        mean_mu = np.mean(mus)
        mean_var = np.mean(variances) + np.mean(np.square(mus)) - np.square(mean_mu)
        return mean_mu, mean_var


    # Create artificial regression dataset where only S out of P feature
    # dimensions contain signal and where there is a single pairwise interaction
    # between the first and second dimensions.
    def toy_data(N=20, S=2, P=10, sigma_obs=0.05):
        assert S < P and P > 1 and S > 0
        onp.random.seed(0)

        X = onp.random.randn(N, P)
        # generate S coefficients with non-negligible magnitude
        W = 0.5 + 2.5 * onp.random.rand(S)
        # generate data using the S coefficients and a single pairwise interaction
        Y = onp.sum(X[:, 0:S] * W, axis=-1) + X[:, 0] * X[:, 1] + sigma_obs * onp.random.randn(N)
        Y -= np.mean(Y)
        Y_std = np.std(Y)

        assert X.shape == (N, P)
        assert Y.shape == (N,)

        return X, Y / Y_std, W / Y_std, 1.0 / Y_std


    # Helper function for analyzing the posterior statistics for coefficient theta_i
    def analyze_dimension(samples, X, Y, dimension, hypers):
        vmap_args = (samples['msq'], samples['lambda'], samples['eta1'], samples['xisq'], samples['var_obs'])
        mus, variances = vmap(lambda msq, lam, eta1, xisq, var_obs:
                              compute_singleton_mean_variance(X, Y, dimension, msq, lam,
                                                              eta1, xisq, hypers['c'], var_obs))(*vmap_args)
        mean, variance = gaussian_mixture_stats(mus, variances)
        std = np.sqrt(variance)
        return mean, std


    # Helper function for analyzing the posterior statistics for coefficient theta_ij
    def analyze_pair_of_dimensions(samples, X, Y, dim1, dim2, hypers):
        vmap_args = (samples['msq'], samples['lambda'], samples['eta1'], samples['xisq'], samples['var_obs'])
        mus, variances = vmap(lambda msq, lam, eta1, xisq, var_obs:
                              compute_pairwise_mean_variance(X, Y, dim1, dim2, msq, lam,
                                                            eta1, xisq, hypers['c'], var_obs))(*vmap_args)
        mean, variance = gaussian_mixture_stats(mus, variances)
        std = np.sqrt(variance)
        return mean, std

    def main(args):
        X, Y, expected_thetas, expected_pairwise = toy_data(N=args.num_data, P=args.num_dimensions,
                                                            S=args.active_dimensions)

        # setup hyperparameters
        hypers = {'expected_sparsity': max(1.0, args.num_dimensions / 10),
                  'alpha1': 3.0, 'beta1': 1.0,
                  'alpha2': 3.0, 'beta2': 1.0,
                  'alpha3': 1.0, 'c': 1.0,
                  'alpha_obs': 3.0, 'beta_obs': 1.0}

        # do inference
        rng_key = random.PRNGKey(0)
        samples = run_inference(model, args, rng_key, X, Y, hypers)

        # compute the mean and square root variance of each coefficient theta_i
        means, stds = vmap(lambda dim: analyze_dimension(samples, X, Y, dim, hypers))(np.arange(args.num_dimensions))

        print("Coefficients theta_1 to theta_%d used to generate the data:" % args.active_dimensions, expected_thetas)
        print("The single quadratic coefficient theta_{1,2} used to generate the data:", expected_pairwise)
        active_dimensions = []

        for dim, (mean, std) in enumerate(zip(means, stds)):
            # we mark the dimension as inactive if the interval [mean - 3 * std, mean + 3 * std] contains zero
            lower, upper = mean - 3.0 * std, mean + 3.0 * std
            inactive = "inactive" if lower < 0.0 and upper > 0.0 else "active"
            if inactive == "active":
                active_dimensions.append(dim)
            print("[dimension %02d/%02d]  %s:\t%.2e +- %.2e" % (dim + 1, args.num_dimensions, inactive, mean, std))

        print("Identified a total of %d active dimensions; expected %d." % (len(active_dimensions),
                                                                            args.active_dimensions))

        # Compute the mean and square root variance of coefficients theta_ij for i,j active dimensions.
        # Note that the resulting numbers are only meaningful for i != j.
        if len(active_dimensions) > 0:
            dim_pairs = np.array(list(itertools.product(active_dimensions, active_dimensions)))
            means, stds = vmap(lambda dim_pair: analyze_pair_of_dimensions(samples, X, Y,
                                                                          dim_pair[0], dim_pair[1], hypers))(dim_pairs)
            for dim_pair, mean, std in zip(dim_pairs, means, stds):
                dim1, dim2 = dim_pair
                if dim1 >= dim2:
                    continue
                lower, upper = mean - 3.0 * std, mean + 3.0 * std
                if not (lower < 0.0 and upper > 0.0):
                    format_str = "Identified pairwise interaction between dimensions %d and %d: %.2e +- %.2e"
                    print(format_str % (dim1 + 1, dim2 + 1, mean, std))

            # Draw a single sample of coefficients theta from the posterior, where we return all singleton
            # coefficients theta_i and pairwise coefficients theta_ij for i, j active dimensions. We use the
            # final MCMC sample obtained from the HMC sampler.
            thetas = sample_theta_space(X, Y, active_dimensions, samples['msq'][-1], samples['lambda'][-1],
                                        samples['eta1'][-1], samples['xisq'][-1], hypers['c'], samples['var_obs'][-1])
            print("Single posterior sample theta:\n", thetas)


    ### X - parameters, Y - data points, {alpha_i, beta_i, c} - hyperparameters, 
    ### N_samps - number of samples for visualization with corner
    def main_posterior(X, Y, hypers, N_samps = 1000, labels=None):

        # args -- needed: num-chains
        # num_dimensions
        # active dimensions
        
        if labels == None:
            labs = [str(_) for _ in range(X.shape[1])]
        else:
            labs = labels
        
        # set up hyperparameters
        hypers = {'expected_sparsity': max(1.0, X.shape[1]/2),
                  'alpha1': alpha1, 'beta1': beta1,
                  'alpha2': alpha2, 'beta2': beta2,
                  'alpha3': alpha3, 'c': c,
                  'alpha_obs': alpha_obs, 'beta_obs': beta_obs}

        # do inference
        rng_key = random.PRNGKey(0)
        samples = run_inference(model, args, rng_key, X, Y, hypers)

        # compute the mean and square root variance of each coefficient theta_i
        means, stds = vmap(lambda dim: analyze_dimension(samples, X, Y, dim, hypers))(np.arange(X.shape[1]))
        num_dims = len(means)
        active_dimensions = []

        for dim, (mean, std) in enumerate(zip(means, stds)):
            # we mark the dimension as inactive if the interval [mean - 3 * std, mean + 3 * std] contains zero
            lower, upper = mean - sigma * std, mean + sigma * std
            inactive = "inactive" if lower < 0.0 and upper > 0.0 else "active"
            if inactive == "active":
                active_dimensions.append(dim)
            print("[dimension %02d/%02d]  %s:\t%.2e +- %.2e" % (dim + 1, X.shape[1], inactive, mean, std))

        print("Identified a total of %d active dimensions." % (len(active_dimensions)))
        
        
        # Compute the mean and square root variance of coefficients theta_ij for i,j active dimensions.
        # Note that the resulting numbers are only meaningful for i != j.
        if len(active_dimensions) > 0:
            
            dim_pairs = np.array(list(itertools.product(active_dimensions, active_dimensions)))
            means, stds = vmap(lambda dim_pair: analyze_pair_of_dimensions(samples, X, Y,
                                                                          dim_pair[0], dim_pair[1], hypers))(dim_pairs)
            # print(dim_pairs)
            dim_pair_arr = []
            dim_pair_index = num_dims -1
            dim_pair_name = []
            pair_labs = []
            for dim_pair, mean, std in zip(dim_pairs, means, stds):
                dim1, dim2 = dim_pair
                if dim1 >= dim2:
                    continue
                dim_pair_index += 1  
                lower, upper = mean - sigma * std, mean + sigma * std
                if not (lower < 0.0 and upper > 0.0):
                    dim_pair_arr.append(dim_pair_index)
                    dim_pair_name.append('%d and %d'%(dim1 + 1, dim2 + 1))
                    format_str = "Identified pairwise interaction between dimensions %d and %d: %.2e +- %.2e"
                    print(format_str % (dim1 + 1, dim2 + 1, mean, std))
                    pair_labs.append(str(labs[dim1]) + ' + ' + str(labs[dim2]))
            
            # Draw a single sample of coefficients theta from the posterior, where we return all singleton
            # coefficients theta_i and pairwise coefficients theta_ij for i, j active dimensions. We use the
            # final MCMC sample obtained from the HMC sampler.
            
            
            ####### ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##########
            ####### ~~~~~~~~~~~~~ CHANGES to the original method ~~~~~~~~~~~~~~~~~~~ #########
            
            ## Get posterior samples from the sample_theta_space_modified() method
            thetas = sample_theta_posterior(X, Y, active_dimensions, samples['msq'][-1], samples['lambda'][-1],
                                                samples['eta1'][-1], samples['xisq'][-1], hypers['c'], 
                                                samples['var_obs'][-1], N_samps, dim_pair_arr)
            print("Active dimensions: " + str(active_dimensions))
            
            ##  Visualize the posterior from the example with corner
            
            labels = ['dim '+str(i) for i in active_dimensions]
            active_dimensions = active_dimensions + dim_pair_arr
            if len(dim_pair_name) != 0:
                for n in range(len(dim_pair_name)):
                    labels.append('dim ' + dim_pair_name[n])
            #fig = corner.corner(thetas, labels = labels);
            return active_dimensions, thetas, labels, pair_labs
        else:
            return active_dimensions, [], []


    def make_corner_plot(thetas, labels):
        fig = corner.corner(thetas, labels=labels)

