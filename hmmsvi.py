#from __future__ import division
#from pympler import tracker, muppy

import numpy as np
import matplotlib.pyplot as plt

from copy import deepcopy

from numpy import newaxis as npa
from scipy.special import digamma, gammaln

from pysvihmm.hmmbase import VariationalHMMBase
from pybasicbayes import distributions as dist

import pysvihmm.util

# This is for taking logs of things so we don't get -inf
eps = 1e-9


class SVIHMM(VariationalHMMBase):
    """ Stochastic variational inference for hidden Markov models.

        obs : observations
        x : hidden states
        init : initial distribution (only useful for multiple series)
        tran : transition matrix
        emit : emission distributions

        The user passes in the hyperparameters for the initial, transition and
        emission distribution. We then store these as hyperparameters, and make
        copies of them to use as the variational parameters, these are the
        parameters we're doing updates on.

        The user should have each unique observation indexed in the emission
        hyperparameters and have those corresponding indexes listed in the
        observations. This way the user wont have to provide a map from the
        indexes to the observations, also it's a lot easier to deal with
        indexes than observations.
    """

    def __init__(self, prior_init, prior_tran, prior_emit, obs):
        """ This initializes the HMMSVI object. Assume we have K states and T
            observations

            prior_init : 1 x K np array containing the prior parameters
                         for the initial distribution.  Use Dirichlet
                         hyperparameters.

            prior_tran : K x K np array containing the prior parameters
                          for the transition distributions. Use K dirichlet
                          hyperparameters (1 for each row).

            prior_emit : K x 1 np array containing the emission
                          distributions, these should be distributions from
                          pybasicbayes/distributions.py

            obs : T x D np array of the observations in D dimensions (Can
                  be a vector if D = 1).
        """

        super(SVIHMM, self).__init__(obs, prior_init, prior_tran, prior_emit)

        self.batch = None

        self.elbo = -np.inf
        self.lrate = 0.1
        self.batchfactor = 1.
        self.N = prior_tran.shape[0]

        # Set the variaitonal hyperparameters, initialized as the
        # hyperparameters input into the model
        self.var_init = prior_init
        self.var_tran = prior_tran
        self.var_emit = prior_emit
        #print("sigma_mf after initialization")
        #print(self.var_emit[0].sigma_mf)


        # We can't set these up until we know the size of a minibatch, M.
        self.var_x = None  # M x K
        self.alpha_table = None  # M x K
        self.beta_table = None  # M x K
        self.c_table = None  # M

        # The modified parameters used in the local update
        self.mod_init = np.zeros(self.K)
        self.mod_tran = np.zeros((self.K, self.K))

        # Checking for memory leaks
        #self.memory_tracker = tracker.SummaryTracker()
        #self.memory_tracker.print_diff()

    def allobs_batch(self):
        """ Generator with one entry that iterates over all observations.
        """
        yield range(self.T)
    
    def update_lrate(self,it):
        return it**2

    def set_var_tran_element(self, val, index_1, index_2):
        self.var_tran[index_1][index_2] = val

    def infer(self, mb_gen, maxit=10):
        """ Runs stochastic variational inference algorithm. This works with
            only a subset of the data.

            mb_gen : Generator to sample minibatches.

            -- We should be able to determine this from the minibatches
            R : This is defined as T / |S| where T is the size of the entire
                dataset and |S| is the size of each sample.
        """

        for it in range(maxit):
            # Sample minibatches
            print(it)
            #print(mb_gen.size/10)
            progress_bar = ["[          ]"]
            i = 1.
            for batch in mb_gen:
                #print("sigma_mf before anything")
                #print(self.var_emit[0].sigma_mf)
                if(not np.all(np.linalg.eigvals(self.var_tran))):
                    raise Exception("Not positive definite")
                step = 1./i
                self.local_update(batch)
                self.global_update(step, batch)

                #print(int(i/(mb_gen.size/10)+1))
                #print("Iteration: " + str(it), '\r')
                #print(str(progress_bar), '\r')
                #if(i%(mb_gen.size/10) == 0):
                    #progress_bar[int(i/(mb_gen.size/10)+1)] = "X"
                i += 1.

    # This must be implemented from hmmbase
    def global_update(self, step, batch=None):
        """ Perform global updates based on batch following the stochastic
            natural gradient.
        """
        lrate = step
        # Perform stochastic gradient update on global params.

        # Initial state distribution -- basically skipping this for now because
        # we can't really handle multiple series.
        self.var_init = np.zeros(self.K)
        self.var_init[0] = 1.
        #print("lrate: " + str(lrate))
        #print("var_tran: " + str(self.var_tran))

        # TODO: Currently these updates compute a gradient from all
        # observations in the minibatch.  However, one could also compute a
        # gradient for each observation individually and average them.  I
        # wonder if the former has less variance but is probabaly more
        # expensive.

        # Transition distributions
        for k in range(self.K):

            # Convert current estimate to natural params
            nats_old = self.var_tran[:,k]

            # Mean-field update
            # Can we move this outside of the for-loop?
            tran_mf = self.prior_tran.copy()
            #print("tran_mf before update: " + str(tran_mf))
            for t in range(1, self.T):
                tran_mf += np.outer(self.var_x[:,t-1], self.var_x[:,t])
            #print("tran_mf after update: " + str(tran_mf))

            # Convert result to natural params
            nats_t = np.squeeze(tran_mf[k,:] - 1.)

            # Perform update according to stochastic gradient
            # (Hoffman, pg. 17)
            nats_new = (1.-lrate)*nats_old + lrate*nats_t
            print("nats_new: " + str(nats_new))
            lrate *= 0.9
            #if(k==1): print("nats_new: " + str(nats_new))

            # Convert results back to moment params
            #self.var_tran[:,k] = nats_new + 1.
            for i in range(self.K):
                self.var_tran[k][i] = nats_new[k] + 1.

        # Emission distributions
        lrate *= 0.5
        for k in range(self.K):

            G = self.var_emit[k]

            # Do mean-field update for this component
            mu_mf, sigma_mf, kappa_mf, nu_mf = \
                pysvihmm.util.NIW_meanfield(G, batch, self.var_x[:,k])

            # Convert to natural parameters
            nats_t = pysvihmm.util.NIW_mf_natural_pars(mu_mf, sigma_mf,
                                                kappa_mf, nu_mf)

            # Convert current estimates to natural parameters
            nats_old = pysvihmm.util.NIW_mf_natural_pars(G.mu_mf, G.sigma_mf,
                                                G.kappa_mf, G.nu_mf)

            # Perform update according to stochastic gradient
            # (Hoffman, pg. 17)
            print("before update: ")
            print(*nats_new)
            nats_new = (1.-lrate)*nats_old + lrate*nats_t
            print("after update: ")
            print(*nats_new)

            # Convert new params into moment form and store back in G
            pysvihmm.util.NIW_mf_moment_pars(G, *nats_new)


    def generate_obs(self, T):
        """ generate_obs will generate T observations using the prior
            hyperparameters given to HMMSVI. It returns the state sequence and
            the observations.

            T : The number of observations to generate.
        """

        sts = []
        curr_st = dist.Categorical(alphav_0=self.prior_init).rvs()
        sts.append(curr_st)

        tran = []
        for i in range(self.N):
            tran.append(dist.Categorical(alphav_0=self.prior_tran[i]))

        obs = []
        obs.append(self.var_emit[curr_st].rvs()[0])

        for t in range(1, T):
            curr_st = tran[curr_st].rvs()
            obs.append(self.var_emit[curr_st].rvs()[0])
            sts.append(curr_st)

        return np.array(sts), np.array(obs)
