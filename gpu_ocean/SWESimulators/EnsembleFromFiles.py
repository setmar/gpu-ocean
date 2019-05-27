# -*- coding: utf-8 -*-

"""
This software is part of GPU Ocean. 

Copyright (C) 2018  SINTEF Digital

This python class implements an ensemble of particles, each consisting
of a single drifter in its own ocean state. Each ocean model is 
perturbed during each timestep, using small scale perturbations.

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


from matplotlib import pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import time
import abc
import warnings 
import os, sys, datetime

import pycuda.driver as cuda

from SWESimulators import BaseOceanStateEnsemble
from SWESimulators import Common
from SWESimulators import CDKLM16
from SWESimulators import Observation
from SWESimulators import DataAssimilationUtils as dautils
from SWESimulators import Observation
from SWESimulators import SimReader 



try:
    from importlib import reload
except:
    pass


class EnsembleFromFiles(BaseOceanStateEnsemble.BaseOceanStateEnsemble):
    """
    Class that holds an ensemble of ocean states, which can be initialized
    from file, and where the true state can also be loaded from file.
    """

    
    def __init__(self, gpu_ctx, numParticles,
                 ensemble_directory,
                 true_state_directory,
                 observation_variance,
                 cont_write_netcdf=False,
                 use_lcg = False):
        """
        Initalizing ensemble from files.
        
        Arguments:
            gpu_ctx: CUDA context
            numParticles: Number of particles/ensemble members
            ensemble_directory: Directory in which NetCDF files defines the ensemble initial conditions.
                If the ensemble size is larger than the number of files with extension ".nc" in the given
                directory, initial states will be duplicated, and we assume that the model error ensures 
                that all particles are different soon enough.
            true_state_directory: Directory which should contain one and only one ".nc" file, which is expected 
                to describe the truth, and one and exactly one ".pickle" file, which should contain observations
                compatible with the Observation class.
            observation_variance: The R matrix. Acceptable forms are a scalar (assuming diagonal R), or 2x2 matrix 
                (assuming block diagonal R, each drifter independent).
            cont_write_netcdf: Flag to write the ensemble to netcdf files in a new directory.
            use_lcg: Flag for using LCG or curand as random number generators for the ensemble members' model errors.
        """
        
        print('Welcome to the EnsembleFromFile')
        print('Ensemble directory: ', ensemble_directory)
        print('True state directory: ', true_state_directory)
        
        self.gpu_ctx = gpu_ctx
        self.gpu_stream = cuda.Stream()
        
        # Control that the ensemble and true state directories exist
        assert os.path.isdir(ensemble_directory), "Ensemble init folder does not exists: " + str(ensemble_directory)
        assert os.path.isdir(true_state_directory), "True state folder does not exists: " + str(true_state_directory)
        
        # List of files for NetCDF ensemble initialization, NetCDF truth, and pickle Observations  
        self.ensemble_init_nc_files = list(os.path.join(ensemble_directory, file) for file in os.listdir(ensemble_directory) if file.endswith('.nc'))
        self.true_state_nc_files = list(os.path.join(true_state_directory, file)  for file in os.listdir(true_state_directory) if file.endswith('.nc'))
        self.observation_files = list(os.path.join(true_state_directory, file)  for file in os.listdir(true_state_directory) if file.endswith('.pickle'))
        
        assert len(self.ensemble_init_nc_files) > 0, "There should be at least one NetCDF file in ensemble directory " + str(ensemble_directory)
        assert len(self.true_state_nc_files) == 1, "There should only be one single NetCDF file in the true state directory " + str(true_state_directory)
        assert len(self.observation_files) == 1,   "There should only be one single pickle file in the true state directory " + str(true_state_directory)
        
        write_netcdf_folder_name = "ensemble_result_" + datetime.datetime.now().strftime("%Y_%m_%d-%H_%M_%S") + "/"
        self.write_netcdf_directory = os.path.join(os.getcwd(), write_netcdf_folder_name)
        print("self.write_netcdf_directory: ", self.write_netcdf_directory)
        
        # Create the particle array
        self.numParticles = numParticles
        self.particles = [None]*(self.numParticles)
        
        # Declare variables for true state and observations
        self.true_state_reader = None
        self.observations = None
        
        # Flag to writing ensemble simulation result to file:
        self.cont_write_netcdf = cont_write_netcdf
        self.use_lcg = use_lcg
        
        # We will not simulate the true state, but read it from file:
        self.simulate_true_state = False
        
        self.observation_type = dautils.ObservationType.UnderlyingFlow
        
        ### Then, call appropriate helper functions for initialization
        self._initializeEnsembleFromFile()
        self._readObservationsFromFile() 
        self._readTruthFromFile() 
        
        #### Set some variables that are used by the super class:
        self.nx, self.ny = self.particles[0].nx, self.particles[0].ny
        self.dx, self.dy = self.particles[0].dx, self.particles[0].dy
        self.dt = self.particles[0].model_time_step
        self.t  = self.particles[0].t
        
        self.driftersPerOceanModel = self.observations.get_num_drifters()
        
        assert(np.isscalar(observation_variance) or observation_variance.shape == (2,2)), 'observation_variance must be scalar or 2x2 matrix'
        if np.isscalar(observation_variance):
            observation_cov = np.diag([observation_variance, observation_variance])
        self.observation_cov = observation_cov.astype(np.float32)
        self.observation_cov_inverse = np.linalg.inv(self.observation_cov).astype(np.float32)
        
        # Store mean water_depth, and whether the equilibrium depth is constant across the domain
        H = self.particles[0].downloadBathymetry()[1][2:-2, 2:-2] # H in cell centers
        self.mean_depth = np.mean(H)
        self.constant_depth = np.max(H) == np.min(H)
        
        
    def _initializeEnsembleFromFile(self):
        num_files = len(self.ensemble_init_nc_files)
        for particle_id in range(self.numParticles):
            file_id = particle_id % num_files
            new_netcdf_filename = None
            if self.cont_write_netcdf or True:
                filename_only = "ensemble_member_" + str(particle_id).zfill(4) + ".nc"
                new_netcdf_filename = os.path.join(self.write_netcdf_directory, filename_only)
            self.particles[particle_id] = CDKLM16.CDKLM16.fromfilename(self.gpu_ctx, 
                                                                       self.ensemble_init_nc_files[file_id],
                                                                       cont_write_netcdf=self.cont_write_netcdf,
                                                                       new_netcdf_filename=new_netcdf_filename,
                                                                       use_lcg=self.use_lcg)
    
    def _readObservationsFromFile(self):
        self.true_state_reader = SimReader.SimNetCDFReader(self.true_state_nc_files[0])
    
    def _readTruthFromFile(self):
        self.observations = Observation.Observation()
        self.observations.read_pickle(self.observation_files[0])

    
        

    def cleanUp(self):
        for particle in self.particles:
            if particle is not None:
                particle.cleanUp()
        
               
    def configureObservations(self, drifterSet="all", observationInterval=1):
        """
        Configuring which drifters we will observe and how often we will observe them.
        
        Arguments:
            drifterSet: can contain the keyword "all" (default), which means that all drifters in the Observation file
                is considered. Otherwise it should be a set of drifter indices represented by a list.
            observationInterval: We choose to consider only every n'th observation.
        """
        if drifterSet != "all":
            self.observations.setDrifterSet(drifterSet)
        self.observations.setObservationInterval(observationInterval)
        self.driftersPerOceanModel = self.observations.get_num_drifters()
        
    def resample(self, newSampleIndices, reinitialization_variance):
        """
        Resampling the particles given by the newSampleIndicies input array.
        Here, the reinitialization_variance input is ignored, meaning that exact
        copies only are resampled.
        """
        obsTrueDrifter = self.observeTrueDrifters()
        positions = self.observeParticles()
        newOceanStates = [None]*self.getNumParticles()
        for i in range(self.getNumParticles()):
            index = newSampleIndices[i]
            
            # Download index's ocean state:
            eta0, hu0, hv0 = self.particles[index].download()
            eta1, hu1, hv1 = self.particles[index].downloadPrevTimestep()
            newOceanStates[i] = (eta0, hu0, hv0, eta1, hu1, hv1)

        # New loop for transferring the correct ocean states back up to the GPU:
        for i in range(self.getNumParticles()):
            self.particles[i].upload(newOceanStates[i][0],
                                     newOceanStates[i][1],
                                     newOceanStates[i][2],
                                     newOceanStates[i][3],
                                     newOceanStates[i][4],
                                     newOceanStates[i][5])
                    
    def step_truth(self):
        raise NotImplementedError("This function should not be used, as the truth is expected to file.")

    def step_particles(self):
        raise NotImplementedError("This class should only use the function stepToObservation, and not step_particles")
               
    def stepToObservation(self, observation_time, model_error_final_step=True, write_now=False):
        """
        Advance the ensemble to the given observation time, and mimics CDKLM16.dataAssimilationStep function
        
        Arguments:
            observation_time: The end time for the simulation
            model_error_final_step: In IEWPF, the model error should not be applied to the final time step.
            write_now: Write result to NetCDF if an writer is active.
            
        """
        for particle in self.particles:
            particle.dataAssimilationStep(observation_time, model_error_final_step=model_error_final_step, write_now=write_now)
        self.t = observation_time
        
        
    def observeTrueDrifters(self):
        return self.observeTrueState()[:, :2]
    
    def observeTrueState(self):
        if not self.constant_depth:
            raise NotImplementedError("observations are not implemented for non-constant equilibrium depths")
        return self.observations.get_observation(self.t, self.mean_depth)
    
    
    def downloadTrueOceanState(self):
        eta, hu, hv, t = self.true_state_reader.getStateAtTime(self.t)
        return eta, hu, hv
    
    
    
    
    
    
    
    
    
        