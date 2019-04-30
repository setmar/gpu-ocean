# -*- coding: utf-8 -*-

"""
This software is part of GPU Ocean. 

Copyright (C) 2019 SINTEF Digital

This python program is used to set up data assimilation experiments.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""


import sys, os, json, datetime, time
import numpy as np

current_dir = os.path.dirname(os.path.realpath(__file__))

if os.path.isdir(os.path.abspath(os.path.join(current_dir, '../../../SWESimulators'))):
        sys.path.insert(0, os.path.abspath(os.path.join(current_dir, '../../../')))


        
#--------------------------------------------------------------
# PARAMETERS
#--------------------------------------------------------------
# Read input parameters and check that they are good

import argparse
parser = argparse.ArgumentParser(description='Generate an ensemble.')
parser.add_argument('-N', '--ensemble_size', type=int, default=None)
parser.add_argument('--method', type=str, default='iewpf2')
parser.add_argument('--observation_interval', type=int, default=1)
parser.add_argument('--observation_variance', type=float, default=15.0)


args = parser.parse_args()

# Checking input args
if args.ensemble_size is None:
    print("Ensemble size missing, please provide a --ensemble_size argument.")
    sys.exit(-1)
elif args.ensemble_size < 1:
    parser.error("Illegal ensemble size " + str(args.ensemble_size))


    
    
    
###-----------------------------------------
## Define files for ensemble and truth.
##
ensemble_init_path = os.path.abspath('double_jet_ensemble_init/')
assert len(os.listdir(ensemble_init_path)) == 102, "Ensemble init folder has wrong number of files"

truth_path = os.path.abspath('double_jet_truth/')
assert len(os.listdir(truth_path)) == 4, "Truth folder has wrong number of files"


timestamp = datetime.datetime.now().strftime("%Y_%m_%d-%H_%M_%S")
destination_dir = os.path.join(current_dir, "da_experiment_" +  timestamp + "/")
os.makedirs(destination_dir)

# Define misc filenames
log_file = os.path.join(destination_dir, 'description.txt')

particleInfoPrefix = os.path.join(destination_dir, 'particle_info_')
forecastFileBase = os.path.join(destination_dir, 'forecast_member_')


with open(log_file, 'w') as f:
    f.write('Data Assimilation experiment ' + timestamp + '\n')
    f.write('----------------------------------------------' + '\n')
    f.write('Input arguments:' + '\n')
    for arg in vars(args):
        f.write('\t' + str((arg, getattr(args, arg))) + '\n')
    f.write('\nPath to initial conditions for ensemble: \n')
    f.write('\t' + ensemble_init_path + '\n')
    f.write('Path to true state:\n')
    f.write('\t' + truth_path + '\n')
    f.write('destination folder:\n')
    f.write('\t' + destination_dir + '\n')
    f.write('Path to particle info:\n')
    f.write('\t' + particleInfoPrefix + '\n')
    f.write('Path to forecast members:\n')
    f.write('\t' + forecastFileBase + '\n')

def log(msg, screen=True):
    with open(log_file, 'a') as f:
        f.write(msg + '\n')
    if screen:
        print(msg)
        
        
# Time parameters
start_time      =  3*24*60*60 #  3 days
simulation_time = 10*24*60*60 # 10 days (three days spin up is prior to this)fa
end_time        = 13*24*60*60 # 13 days

drifterSet = [4, 12, 20, 28, 36, 44, 52, 60]
extraCells = np.array([[423,  25],
                       [381,  27],
                       [185,  48],
                       [ 69, 157],
                       [288, 132],
                       [331, 177],
                       [205, 201],
                       [442, 234],
                       [ 93,  11],
                       [462,   0],
                       [202, 135],
                       [405, 135],
                       [315, 229]])



###--------------------------------
# Import required packages
#
tic = time.time()
# For GPU contex:
from SWESimulators import Common
# For the ensemble:
from SWESimulators import EnsembleFromFiles, Observation, ParticleInfo
# For data assimilation:
from SWESimulators import IEWPFOcean
# For forcasting:
from SWESimulators import GPUDrifterCollection

toc = time.time()
log("\n{:02.4f} s: ".format(toc-tic) + 'GPU Ocean packages imported', True)

# Create CUDA context
tic = time.time()
gpu_ctx = Common.CUDAContext()
device_name = gpu_ctx.cuda_device.name()
toc = time.time()
log("{:02.4f} s: ".format(toc-tic) + "Created context on " + device_name, True)


###--------------------------
# Initiate the ensemble
#
tic = time.time()
ensemble = EnsembleFromFiles.EnsembleFromFiles(gpu_ctx, args.ensemble_size, \
                                               ensemble_init_path, truth_path, \
                                               args.observation_variance)#,
                                               #cont_write_netcdf = True,
                                               #write_netcdf_directory = destination_dir)

# Configure observations according to the selected drifters:
ensemble.configureObservations(drifterSet=drifterSet, observationInterval = args.observation_interval)
ensemble.configureParticleInfos(extraCells)
toc = time.time()
log("{:02.4f} s: ".format(toc-tic) + "Ensemble is loaded and created", True)

### -------------------------------
# Initialize IEWPF class (if needed)
#
tic = time.time()
iewpf = None
if str(args.method).lower().startswith('iewpf'):
    iewpf = IEWPFOcean.IEWPFOcean(ensemble)
    toc = time.time()
    log("{:02.4f} s: ".format(toc-tic) + "Data assimilation class initiated", True)
else:
    toc = time.time()
    log("{:02.4f} s: ".format(toc-tic) + "Skipping creation of IEWPF as the method is not used", True)

    

    

### ----------------------------------------------
#   DATA ASSIMILATION
#
log('---------- Starting simulation --------------') 

endtime = 3*24*60*60

master_tic = time.time()

numDays = 1
numHours = 4

for day in days(numDays):
    log('-------- Starting day ' + str(day))
    
    for hour in range(numHours):
        
        for fiveMin in range(12):
            
            drifter_cells = ensemble.getDrifterCells()
            
            for min in range(5):
                endtime += 60
                ensemble.stepToObservation(endtime, model_error_final_step=(m<4))
                
                if m == 4:
                    iewpf.iewpf_2stage(ensemble, perform_step=False)
                
                ensemble.registerStateSample(drifter_cells)
            # Done minutes
            
        # Done five minutes
    
        toc = time.time()
        log("{:04.1f} s: ".format(toc-master_tic) + " Done simulating hour " + str(hour + 1) + " of day " + (day + 3))
    # Done hours

    ensemble.dumpParticleInfosToFile(particleInfoPrefix)
    
    # TODO: Write netcdf
    
# Done days



### -------------------------------------------------
#   Start forecast
#


log('-----------------------------------------------------------')
log('------   STARTING FORECAST                   --------------')
log('-----------------------------------------------------------')

forecast_start_time = endtime
drifter_start_positions = ensemble.observeTrueDrifters()

forecast_end_time = forecast_start_time + 1*24*60*60

observation_intervals = 5*60
observations_iterations = int((forecast_end_time - forecast_start_time)/observation_intervals)

particle_id = 0

t = sim.t

for sim in ensemble.particles:
    
    tic = time.time()


    drifters = GPUDrifterCollection.GPUDrifterCollection(gpu_ctx, num_drifters,
                                                         boundaryConditions=ensemble.getBoundaryConditions(), 
                                                         domain_size_x=ensemble.getDomainSizeX(), domain_size_y=ensemble.getDomainSizeY())
    drifters.setDrifterPositions(drifter_start_positions)
    sim.attachDrifters(drifters)

    forecast_file_name = forecastFileBase + str(particle_id).zfill(4) + ".bz2"

    observations = Observation.Observation()
    observations.add_observation_from_sim(sim)


    for obs_it in range(observations_iterations):
        next_obs_time = t + observation_frequency

        # Step until next observation 
        sim.dataAssimilationStep(next_obs_time, write_now=False)

        # Store observation
        observations.add_observation_from_sim(sim)


    # TODO! Write to netcdf also!
        
    # Write forecast to file    
    observations.to_pickle(forecast_file_name)

    toc = time.time()
    log("{:04.1f} s: ".format(toc-tic) + " Forecast for particle " + str(particle_id) + " done")
    log("      Forecast written to " + forecast_file_name)

    particle_id += 1

            
# Clean up simulation and close netcdf file
tic = time.time()
sim = None
ensemble.cleanUp()
toc = time.time()
print("\n{:02.4f} s: ".format(toc-tic) + "Clean up simulator done.")

log('Done! Only checking is left. There should be a "yes, done" in the next line')


assert(numDays == 7), 'Simulated with wrong number of days!'
assert(numHours == 24), 'Simulated with wrong number of hours'
assert(forecast_end_time == 13*24*60*60), 'Forecast did not reach goal time'

log('Yes, done!')


exit(0)




