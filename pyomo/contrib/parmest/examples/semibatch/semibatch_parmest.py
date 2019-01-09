import numpy as np
import pandas as pd
import json
import pyomo.contrib.parmest.parmest as parmest
from semibatch import generate_model

### Parameter estimation

# Vars to estimate
theta_names = ['k1', 'k2', 'E1', 'E2']

# Data, list of dictionaries
data = [] 
for exp_num in range(10):
    fname = 'exp'+str(exp_num+1)+'.out'
    with open(fname,'r') as infile:
        d = json.load(infile)
        data.append(d)

# Note, the model already includes a 'SecondStageCost' expression 
# for sum of squared error that will be used in parameter estimation
        
pest = parmest.Estimator(generate_model, data, theta_names)
obj, theta = pest.theta_est()
print(obj)
print(theta)

### Parameter estimation with bootstrap resampling

np.random.seed(95264)
bootstrap_theta = pest.theta_est_bootstrap(50)
print(bootstrap_theta.head())

parmest.pairwise_plot(bootstrap_theta, theta, 'rectangular', 0.8)
mvn_dist = parmest.pairwise_plot(bootstrap_theta, theta, 'multivariate_normal', 0.8)
kde_dist = parmest.pairwise_plot(bootstrap_theta, theta, 'gaussian_kde', 0.8)

### Parameter estimation with likelihood ratio

theta_vals = pd.DataFrame(columns=theta_names)
i = 0
for E2 in np.arange(38000, 42000, 500):
    for k2 in np.arange(40, 160, 40):
        theta_vals.loc[i,:] = [19, k2, 30524, E2]
        i = i+1
obj_at_theta = pest.objective_at_theta(theta_vals)
print(obj_at_theta)
LR = pest.likelihood_ratio_test(obj_at_theta, obj, [0.8, 0.85, 0.9, 0.95])
print(LR.head())

LR80 = LR.loc[LR[0.8] == True, theta_names]
parmest.pairwise_plot(LR80)
