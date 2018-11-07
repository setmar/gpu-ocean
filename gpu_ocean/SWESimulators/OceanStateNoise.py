# -*- coding: utf-8 -*-

"""
This python class produces random perturbations that are to be added to 
the ocean state fields in order to generate model error.

Copyright (C) 2018  SINTEF ICT

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
import numpy as np
import gc

from SWESimulators import Common
from SWESimulators import config
from SWESimulators import FBL, CTCS

class OceanStateNoise(object):
    """
    Generating random perturbations for a ocean state.
   
    Perturbation for the surface field, dEta, is produced with a covariance structure according to a SOAR function,
    while dHu and dHv are found by the geostrophic balance to avoid shock solutions.
    """
    
    def __init__(self, gpu_ctx, gpu_stream,
                 nx, ny, dx, dy,
                 boundaryConditions, staggered,
                 soar_q0=None, soar_L=None,
                 interpolation_factor = 1,
                 block_width=16, block_height=16):
        """
        Initiates a class that generates small scale geostrophically balanced perturbations of
        the ocean state.
        (nx, ny): number of internal grid cells in the domain
        (dx, dy): size of each grid cell
        soar_q0: amplitude parameter for the perturbation, default: dx*1e-5
        soar_L: length scale of the perturbation covariance, default: 0.74*dx*interpolation_factor
        interpolation_factor: indicates that the perturbation of eta should be generated on a coarse mesh, 
            and then interpolated down to the computational mesh. The coarse mesh will then have
            (nx/interpolation_factor, ny/interpolation_factor) grid cells.
        (block_width, block_height): The size of each GPU block
        """

        self.random_state = np.random.RandomState()
        
        self.random_numbers = None
        self.seed = None
        
        self.gpu_ctx = gpu_ctx
        self.gpu_stream = gpu_stream
        
        self.nx = np.int32(nx)
        self.ny = np.int32(ny)
        self.dx = np.float32(dx)
        self.dy = np.float32(dy)
        self.staggered = np.int(0)
        if staggered:
            self.staggered = np.int(1)
            
        # The cutoff parameter is hard-coded.
        # The size of the cutoff determines the computational radius in the
        # SOAR function. Hence, the size of the local memory in the OpenCL 
        # kernels has to be hard-coded.
        self.cutoff = np.int32(config.soar_cutoff) 
        
        
        # Check that the interpolation factor plays well with the grid size:
        assert ( interpolation_factor > 0 and interpolation_factor % 2 == 1), 'interpolation_factor must be a positive odd integer'
        
        assert (nx % interpolation_factor == 0), 'nx must be divisible by the interpolation factor'
        assert (ny % interpolation_factor == 0), 'ny must be divisible by the interpolation factor'
        self.interpolation_factor = np.int32(interpolation_factor)
        
        # The size of the coarse grid 
        self.coarse_nx = np.int32(nx/self.interpolation_factor)
        self.coarse_ny = np.int32(ny/self.interpolation_factor)
        self.coarse_dx = np.float32(dx*self.interpolation_factor)
        self.coarse_dy = np.float32(dy*self.interpolation_factor)
        
        self.periodicNorthSouth = np.int32(boundaryConditions.isPeriodicNorthSouth())
        self.periodicEastWest = np.int32(boundaryConditions.isPeriodicEastWest())
        
        # Size of random field and seed
        # The SOAR function is a stencil which requires cutoff number of grid cells,
        # and the interpolation operator requires further 2 ghost cell values in each direction.
        # The random field must therefore be created with 2 + cutoff number of ghost cells.
        self.rand_ghost_cells_x = np.int32(2+self.cutoff)
        self.rand_ghost_cells_y = np.int32(2+self.cutoff)
        if self.periodicEastWest:
            self.rand_ghost_cells_x = np.int32(0)
        if self.periodicNorthSouth:
            self.rand_ghost_cells_y = np.int32(0)
        self.rand_nx = np.int32(self.coarse_nx + 2*self.rand_ghost_cells_x)
        self.rand_ny = np.int32(self.coarse_ny + 2*self.rand_ghost_cells_y)
            
        # Since normal distributed numbers are generated in pairs, we need to store half the number of
        # of seed values compared to the number of random numbers.
        self.seed_ny = np.int32(self.rand_ny)
        self.seed_nx = np.int32(np.ceil(self.rand_nx/2)) 
        
        
        # Constants for the SOAR function:
        self.soar_q0 = np.float32(self.dx/100000)
        if soar_q0 is not None:
            self.soar_q0 = np.float32(soar_q0)
            
        self.soar_L = np.float32(0.75*self.coarse_dx)
        if soar_L is not None:
            self.soar_L = np.float32(soar_L)
        
        # Generate seed:
        self.floatMax = 2147483648.0
        self.host_seed = self.random_state.rand(self.seed_ny, self.seed_nx)*self.floatMax
        self.host_seed = self.host_seed.astype(np.uint64, order='C')

        self.seed = Common.CUDAArray2D(gpu_stream, self.seed_nx, self.seed_ny, 0, 0, self.host_seed, double_precision=True, integers=True)
        
        # Allocate memory for random numbers
        self.random_numbers_host = np.zeros((self.rand_ny, self.rand_nx), dtype=np.float32, order='C')
        self.random_numbers = Common.CUDAArray2D(self.gpu_stream, self.rand_nx, self.rand_ny, 0, 0, self.random_numbers_host)
        
        # Allocate memory for coarse buffer if needed
        # Two ghost cells in each direction needed for bicubic interpolation 
        self.coarse_buffer_host = np.zeros((self.coarse_ny+4, self.coarse_nx+4), dtype=np.float32, order='C')
        self.coarse_buffer = Common.CUDAArray2D(self.gpu_stream, self.coarse_nx, self.coarse_ny, 2, 2, self.coarse_buffer_host)

        
        # Generate kernels
        self.kernels = gpu_ctx.get_kernel("ocean_noise.cu", \
                                          defines={'block_width': block_width, 'block_height': block_height})
        
        
        # Get CUDA functions and define data types for prepared_{async_}call()
        self.uniformDistributionKernel = self.kernels.get_function("uniformDistribution")
        self.uniformDistributionKernel.prepare("iiiPiPi")
        
        self.normalDistributionKernel = self.kernels.get_function("normalDistribution")
        self.normalDistributionKernel.prepare("iiiPiPi")
        
        self.soarKernel = self.kernels.get_function("SOAR")
        self.soarKernel.prepare("iifffffiiPiPi")
        
        self.geostrophicBalanceKernel = self.kernels.get_function("geostrophicBalance")
        self.geostrophicBalanceKernel.prepare("iiffiiffffPiPiPiPiPi")
        
        self.bicubicInterpolationKernel = self.kernels.get_function("bicubicInterpolation")
        self.bicubicInterpolationKernel.prepare("iiiiffiiiiffiiffffPiPiPiPiPi")
        
        #Compute kernel launch parameters
        self.local_size = (block_width, block_height, 1)
        
        # Launch one thread for each seed, which in turns generates two iid N(0,1)
        self.global_size_random_numbers = ( \
                       int(np.ceil(self.seed_nx / float(self.local_size[0]))), \
                       int(np.ceil(self.seed_ny / float(self.local_size[1]))) \
                                  ) 
        
        # Launch one thread per SOAR-correlated result - need to write to two ghost 
        # cells in order to do bicubic interpolation based on the result
        self.global_size_SOAR = ( \
                                 int(np.ceil( (self.coarse_nx+4)/float(self.local_size[0]))), \
                                 int(np.ceil( (self.coarse_ny+4)/float(self.local_size[1]))) \
                                )
        
        # One thread per resulting perturbed grid cell
        self.global_size_geo_balance = ( \
                                        int(np.ceil( (self.nx)/float(self.local_size[0]))), \
                                        int(np.ceil( (self.ny)/float(self.local_size[1]))) \
                                       )
        
        
        
    def __del__(self):
        self.cleanUp()
     
    def cleanUp(self):
        if self.seed is not None:
            self.seed.release()
        if self.random_numbers is not None:
            self.random_numbers.release()
        self.gpu_ctx = None
        gc.collect()
        
    @classmethod
    def fromsim(cls, sim, soar_q0=None, soar_L=None, interpolation_factor=1,  
                block_width=16, block_height=16):
        staggered = False
        if isinstance(sim, FBL.FBL) or isinstance(sim, CTCS.CTCS):
            staggered = True
        return cls(sim.gpu_ctx, sim.gpu_stream,
                   sim.nx, sim.ny, sim.dx, sim.dy,
                   sim.boundary_conditions, staggered,
                   soar_q0=soar_q0, soar_L=soar_L,
                   interpolation_factor=interpolation_factor,
                   block_width=block_width, block_height=block_height)
        
        
        
    def getSeed(self):
        return self.seed.download(self.gpu_stream)
    
    def resetSeed(self):
        # Generate seed:
        self.floatMax = 2147483648.0
        self.host_seed = self.random_state.rand(self.seed_ny, self.seed_nx)*self.floatMax
        self.host_seed = self.host_seed.astype(np.uint64, order='C')
        self.seed.upload(self.gpu_stream, self.host_seed)
        
    
    def getRandomNumbers(self):
        return self.random_numbers.download(self.gpu_stream)
    
    def getCoarseBuffer(self):
        return self.coarse_buffer.download(self.gpu_stream)
    
    def generateNormalDistribution(self):
        self.normalDistributionKernel.prepared_async_call(self.global_size_random_numbers, self.local_size, self.gpu_stream, 
                                                          self.seed_nx, self.seed_ny,
                                                          self.rand_nx,
                                                          self.seed.data.gpudata, self.seed.pitch,
                                                          self.random_numbers.data.gpudata, self.random_numbers.pitch)
        
    def generateUniformDistribution(self):
        # Call kernel -> new random numbers
        self.uniformDistributionKernel.prepared_async_call(self.global_size_random_numbers, self.local_size, self.gpu_stream,
                                                           self.seed_nx, self.seed_ny,
                                                           self.rand_nx,
                                                           self.seed.data.gpudata, self.seed.pitch,
                                                           self.random_numbers.data.gpudata, self.random_numbers.pitch)
    
    def perturbSim(self, sim, q0_scale=1.0, update_random_field=True, perturbation_scale=1.0,
                   align_with_cell_i=None, align_with_cell_j=None):
        """
        Generating a perturbed ocean state and adding it to sim's ocean state 
        """
        
        self.perturbOceanState(sim.gpu_data.h0, sim.gpu_data.hu0, sim.gpu_data.hv0,
                               sim.bathymetry.Bi,
                               sim.f, beta=sim.coriolis_beta, g=sim.g, 
                               y0_reference_cell=sim.y_zero_reference_cell,
                               ghost_cells_x=sim.ghost_cells_x,
                               ghost_cells_y=sim.ghost_cells_y,
                               q0_scale=q0_scale,
                               update_random_field=update_random_field,
                               align_with_cell_i=align_with_cell_i,
                               align_with_cell_j=align_with_cell_j)
                               
    
    def perturbOceanState(self, eta, hu, hv, H, f, beta=0.0, g=9.81, 
                          y0_reference_cell=0, ghost_cells_x=0, ghost_cells_y=0,
                          q0_scale=1.0, update_random_field=True, perturbation_scale=1.0,
                          align_with_cell_i=None, align_with_cell_j=None):
        """
        Apply the SOAR Q covariance matrix on the random ocean field which is
        added to the provided buffers eta, hu and hv.
        eta: surface deviation - CUDAArray2D object.
        hu: volume transport in x-direction - CUDAArray2D object.
        hv: volume transport in y-dirextion - CUDAArray2D object.
        
        Optional parameters not used else_where:
        q0_scale=1: scale factor to the SOAR amplitude parameter q0
        update_random_field=True: whether to generate new random numbers or use those already 
            present in the random numbers buffer
        perturbation_scale=1: scale factor to the perturbation of the eta field
        align_with_cell_i=None, align_with_cell_j=None: Index to a cell for which to align the coarse grid
        """
        if update_random_field:
            # Need to update the random field, requiering a global sync
            self.generateNormalDistribution()
        
        soar_q0 = np.float32(self.soar_q0 * q0_scale)
        
        offset_i, offset_j = self._obtain_coarse_grid_offset(align_with_cell_i, align_with_cell_j)
        
        # Generate the SOAR field on the coarse grid
        
        
        self.soarKernel.prepared_async_call(self.global_size_SOAR, self.local_size, self.gpu_stream,
                                            self.coarse_nx, self.coarse_ny,
                                            self.coarse_dx, self.coarse_dy,

                                            soar_q0, self.soar_L,
                                            np.float32(perturbation_scale),
                                            
                                            self.periodicNorthSouth, self.periodicEastWest,
                                            self.random_numbers.data.gpudata, self.random_numbers.pitch,
                                            self.coarse_buffer.data.gpudata, self.coarse_buffer.pitch)
        
        if self.interpolation_factor > 1:
            self.bicubicInterpolationKernel.prepared_async_call(self.global_size_geo_balance, self.local_size, self.gpu_stream,
                                                                self.nx, self.ny, 
                                                                np.int32(ghost_cells_x), np.int32(ghost_cells_y),
                                                                self.dx, self.dy,
                                                                
                                                                self.coarse_nx, self.coarse_ny,
                                                                np.int32(ghost_cells_x), np.int32(ghost_cells_y),
                                                                self.coarse_dx, self.coarse_dy,
                                                                np.int32(offset_i), np.int32(offset_j),
                                                                
                                                                np.float32(g), np.float32(f),
                                                                np.float32(beta), np.float32(y0_reference_cell),
                                                                
                                                                self.coarse_buffer.data.gpudata, self.coarse_buffer.pitch,
                                                                eta.data.gpudata, eta.pitch,
                                                                hu.data.gpudata, hu.pitch,
                                                                hv.data.gpudata, hv.pitch,
                                                                H.data.gpudata, H.pitch)

        else:
            self.geostrophicBalanceKernel.prepared_async_call(self.global_size_geo_balance, self.local_size, self.gpu_stream,
                                                              self.nx, self.ny,
                                                              self.dx, self.dy,
                                                              np.int32(ghost_cells_x), np.int32(ghost_cells_y),

                                                              np.float32(g), np.float32(f),
                                                              np.float32(beta), np.float32(y0_reference_cell),

                                                              self.coarse_buffer.data.gpudata, self.coarse_buffer.pitch,
                                                              eta.data.gpudata, eta.pitch,
                                                              hu.data.gpudata, hu.pitch,
                                                              hv.data.gpudata, hv.pitch,
                                                              H.data.gpudata, H.pitch)
    
    def _obtain_coarse_grid_offset(self, fine_index_i, fine_index_j):
        
        default_offset = self.interpolation_factor//2

        offset_i, offset_j = 0, 0
        
        if fine_index_i is not None:
            coarse_i = fine_index_i//self.interpolation_factor
            raw_offset_i = fine_index_i % self.interpolation_factor
            offset_i = int(raw_offset_i - default_offset)
        if fine_index_j is not None:        
            coarse_j = fine_index_j//self.interpolation_factor
            raw_offset_j = fine_index_j % self.interpolation_factor
            offset_j = int(raw_offset_j - default_offset)
        return offset_i, offset_j

    
    ##### CPU versions of the above functions ####
    
    def getSeedCPU(self):
        return self.host_seed
    
    def generateNormalDistributionCPU(self):
        self._CPUUpdateRandom(True)
    
    def generateUniformDistributionCPU(self):
        self._CPUUpdateRandom(False)
    
    def getRandomNumbersCPU(self):
        return self.random_numbers_host
    
    def perturbEtaCPU(self, eta, use_existing_GPU_random_numbers=False,
                      ghost_cells_x=0, ghost_cells_y=0):
        """
        Apply the SOAR Q covariance matrix on the random field to add
        a perturbation to the incomming eta buffer.
        eta: numpy array
        """
        # Call CPU utility function
        if use_existing_GPU_random_numbers:
            self.random_numbers_host = self.getRandomNumbers()
        else:
            self.generateNormalDistributionCPU()
        d_eta = self._applyQ_CPU()
        
        if self.interpolation_factor > 1:
            d_eta = self._interpolate_CPU(d_eta, geostrophic_balance=False)
        
        interior = [-ghost_cells_y, -ghost_cells_x, ghost_cells_y, ghost_cells_x]
        for i in range(4):
            if interior[i] == 0:
                interior[i] = None
        
        eta[interior[2]:interior[0], interior[3]:interior[1]] = d_eta[2:-2, 2:-2]
    
    def perturbOceanStateCPU(self, eta, hu, hv, H, f,  beta=0.0, g=9.81,
                             ghost_cells_x=0, ghost_cells_y=0,
                             use_existing_GPU_random_numbers=False):
        """
        Apply the SOAR Q covariance matrix on the random field to add
        a perturbation to the incomming eta buffer.
        Generate geostrophically balanced hu and hv which is added to the incomming hu and hv buffers.
        eta: numpy array
        """
        # Call CPU utility function
        if use_existing_GPU_random_numbers:
            self.random_numbers_host = self.getRandomNumbers()
        else:
            self.generateNormalDistributionCPU()
        
        # generates perturbation (d_eta[ny+4, nx+4], d_hu[ny, nx] and d_hv[ny, nx])
        d_eta, d_hu, d_hv = self._obtainOceanPerturbations_CPU(H, f, beta, g)
        
        interior = [-ghost_cells_y, -ghost_cells_x, ghost_cells_y, ghost_cells_x]
        for i in range(4):
            if interior[i] == 0:
                interior[i] = None
        
        eta[interior[2]:interior[0], interior[3]:interior[1]] += d_eta[2:-2, 2:-2]
        hu[interior[2]:interior[0], interior[3]:interior[1]] += d_hu
        hv[interior[2]:interior[0], interior[3]:interior[1]] += d_hv
    
    
     
    
    # ------------------------------
    # CPU utility functions:
    # ------------------------------
    
    def _lcg(self, seed):
        modulo = np.uint64(2147483647)
        seed = np.uint64(((seed*1103515245) + 12345) % modulo) #0x7fffffff
        return seed / 2147483648.0, seed
    
    def _boxMuller(self, seed_in):
        seed = np.uint64(seed_in)
        u1, seed = self._lcg(seed)
        u2, seed = self._lcg(seed)
        r = np.sqrt(-2.0*np.log(u1))
        theta = 2*np.pi*u2
        n1 = r*np.cos(theta)
        n2 = r*np.sin(theta)
        return n1, n2, seed
    
    def _CPUUpdateRandom(self, normalDist):
        """
        Updating the random number buffer at the CPU.
        normalDist: Boolean parameter. 
            If True, the random numbers are from N(0,1)
            If False, the random numbers are from U[0,1]
        """
        #(ny, nx) = seed.shape
        #(domain_ny, domain_nx) = random.shape
        b_dim_x = self.local_size[0]
        b_dim_y = self.local_size[1]
        blocks_x = self.global_size_random_numbers[0]
        blocks_y = self.global_size_random_numbers[1]
        for by in range(blocks_y):
            for bx in range(blocks_x):
                for j in range(b_dim_y):
                    for i in range(b_dim_x):

                        ## Content of kernel:
                        y = b_dim_y*by + j # thread_id
                        x = b_dim_x*bx + i # thread_id
                        if (x < self.seed_nx and y < self.seed_ny):
                            n1, n2 = 0.0, 0.0
                            if normalDist:
                                n1, n2, self.host_seed[y,x]   = self._boxMuller(self.host_seed[y,x])
                            else:
                                n1, self.host_seed[y,x] = self._lcg(self.host_seed[y,x])
                                n2, self.host_seed[y,x] = self._lcg(self.host_seed[y,x])
                                
                            if x*2 + 1 < self.rand_nx:
                                self.random_numbers_host[y, x*2  ] = n1
                                self.random_numbers_host[y, x*2+1] = n2
                            elif x*2 == self.rand_nx:
                                self.random_numbers_host[y, x*2] = n1
    
    def _SOAR_Q_CPU(self, a_x, a_y, b_x, b_y):
        """
        CPU implementation of a SOAR covariance function between grid points
        (a_x, a_y) and (b_x, b_y)
        """
        dist = np.sqrt(  self.coarse_dx*self.coarse_dx*(a_x - b_x)**2  
                       + self.coarse_dy*self.coarse_dy*(a_y - b_y)**2 )
        return self.soar_q0*(1.0 + dist/self.soar_L)*np.exp(-dist/self.soar_L)
    
    def _applyQ_CPU(self, perturbation_scale=1):
        #xi, dx=1, dy=1, q0=0.1, L=1, cutoff=5):
        """
        Create the perturbation field for eta based on the SOAR covariance 
        structure.
        
        The resulting size is (coarse_nx+4, coarse_ny+4), as two ghost cells are required to 
        do bicubic interpolation of the result.
        """
                        
        # Assume in a GPU setting - we read xi into shared memory with ghostcells
        # Additional cutoff number of ghost cells required to calculate SOAR contribution
        ny_halo = int(self.coarse_ny + (2 + self.cutoff)*2)
        nx_halo = int(self.coarse_nx + (2 + self.cutoff)*2)
        local_xi = np.zeros((ny_halo, nx_halo))
        for j in range(ny_halo):
            global_j = j
            if self.periodicNorthSouth:
                global_j = (j - self.cutoff - 2) % self.rand_ny
            for i in range(nx_halo):
                global_i = i
                if self.periodicEastWest:
                    global_i = (i - self.cutoff - 2) % self.rand_nx
                local_xi[j,i] = self.random_numbers_host[global_j, global_i]
                
        # Sync threads
        
        # Allocate output buffer
        Qxi = np.zeros((self.coarse_ny+4, self.coarse_nx+4))
        for a_y in range(self.coarse_ny+4):
            for a_x in range(self.coarse_nx+4):
                # This is a OpenCL thread (a_x, a_y)
                local_a_x = a_x + self.cutoff
                local_a_y = a_y + self.cutoff
                
                #############
                #Qxi[a_y, a_x] = local_xi[local_a_y, local_a_x]
                #continue
                #############
                
                
                start_b_y = local_a_y - self.cutoff
                end_b_y =  local_a_y + self.cutoff+1
                start_b_x = local_a_x - self.cutoff
                end_b_x =  local_a_x + self.cutoff+1

                Qx = 0.0
                for b_y in range(start_b_y, end_b_y):
                    for b_x in range(start_b_x, end_b_x):
                        Q = self._SOAR_Q_CPU(local_a_x, local_a_y, b_x, b_y)
                        Qx += Q*local_xi[b_y, b_x]
                Qxi[a_y, a_x] = perturbation_scale*Qx
        
        return Qxi
    
    
    def _obtainOceanPerturbations_CPU(self, H, f, beta, g, perturbation_scale=1):
        # Obtain perturbed eta - size (coarse_ny+4, coarse_nx+4)
        d_eta = self._applyQ_CPU(perturbation_scale)

        # Interpolate if the coarse grid is not the same as the computational grid
        # d_eta then becomes (ny+4, nx+4)
        if self.interpolation_factor > 1:
            d_eta = self._interpolate_CPU(d_eta)
        
        ####
        # Global sync (currently)
        #     Can be made into a local sync, as long as d_eta is given 
        #     periodic overlap (1 more global computated ghost cell)
        ####

        d_hu = np.zeros((self.ny, self.nx))
        d_hv = np.zeros((self.ny, self.nx))

        ### Find H_mid:
        # Read global H (def on intersections) to local, find H_mid
        # The local memory can then be reused to something else (perhaps use local_d_eta before computing local_d_eta?)
        H_mid = np.zeros((self.ny, self.nx))
        for j in range(self.ny):
            for i in range(self.nx):
                H_mid[j,i] = 0.25* (H[j,i] + H[j+1, i] + H[j, i+1] + H[j+1, i+1])
        
        ####
        # Local sync
        ####

        # Compute geostrophically balanced (hu, hv) for each cell within the domain
        for j in range(0, self.ny):
            local_j = j + 2     # index in d_eta buffer
            coriolis = f + beta*local_j*self.dy
            for i in range(0, self.nx):
                local_i = i + 2    # index in d_eta buffer
                h_mid = d_eta[local_j,local_i] + H_mid[j, i]
                
                ##############
                #h_mid = H_mid[j, i]
                ##############
                
                
                eta_diff_y = (d_eta[local_j+1, local_i] - d_eta[local_j-1, local_i])/(2.0*self.dy)
                d_hu[j,i] = -(g/coriolis)*h_mid*eta_diff_y

                eta_diff_x = (d_eta[local_j, local_i+1] - d_eta[local_j, local_i-1])/(2.0*self.dx)
                d_hv[j,i] = (g/coriolis)*h_mid*eta_diff_x   
    
        return d_eta, d_hu, d_hv
    
    
    
    def _interpolate_CPU(self, coarse_eta, interpolation_order=3):
        """
        Interpolates values coarse_eta defined on the coarse grid onto the computational grid.
        Input coarse_eta is of size [coarse_ny+4, coarse_nx+4], and output will be given as
        eta [ny+4, nx+4].
        """

        
        # Create buffers for eta, hu and hv:
        d_eta = np.zeros((self.ny+4, self.nx+4))
      
        
        
        
        min_rel_x = 10
        max_rel_x = -10
        min_rel_y = 10
        max_rel_y = -10
        
        # Loop over internal cells and first ghost cell layer.
        for loc_j in range(self.ny+2):
            for loc_i in range(self.nx+2):
                
                # index in resulting d_eta buffer
                i = loc_i + 1
                j = loc_j + 1

                # Position of cell center in fine grid:
                x = (i - 2 + 0.5)*self.dx
                y = (j - 2 + 0.5)*self.dy

                # Location in coarse grid (defined in course grid's cell centers)
                # (coarse_i, coarse_j) is the first coarse grid point towards lower left.
                coarse_i = int(np.floor(x/self.coarse_dx + 2 - 0.5))
                coarse_j = int(np.floor(y/self.coarse_dy + 2 - 0.5))
                
                # Position of the coarse grid point
                coarse_x = (coarse_i - 2 + 0.5)*self.coarse_dx
                coarse_y = (coarse_j - 2 + 0.5)*self.coarse_dy
                
                assert coarse_x <= x
                assert coarse_x + self.coarse_dx >= x

                rel_x = (x - coarse_x)/self.coarse_dx
                rel_y = (y - coarse_y)/self.coarse_dy
                
                if rel_x < min_rel_x:
                    min_rel_x = rel_x
                if rel_x > max_rel_x:
                    max_rel_x = rel_x
                if rel_y < min_rel_y:
                    min_rel_y = rel_y
                if rel_y > max_rel_y:
                    max_rel_y = rel_y

                assert rel_x >= 0 and rel_x < 1
                assert rel_y >= 0 and rel_y < 1
                
                # Defining the coarse grid points on coarse intersections rather than in grid centers.
                #coarse_i = int(np.floor(x/coarse_dx + 1))
                #coarse_j = int(np.floor(y/coarse_dy + 1))
                #coarse_x = (coarse_i - 1)*coarse_dx 
                #coarse_y = (coarse_j - 1)*coarse_dy 

                #print ("(i, x, coarse_i, coarse_x)", (i, x, coarse_i, coarse_x))
                #if loc_j == 0:
                #    print ("--> (i, x, coarse_i, coarse_x)", (i, x, coarse_i, coarse_x))
                #if loc_i == 0:
                #    print ("--> (j, y, coarse_j, coarse_y)", (j, y, coarse_j, coarse_y))
                    
                d_eta[j,i] = self._bicubic_interpolation_inner(coarse_eta, coarse_i, coarse_j, rel_x, rel_y, interpolation_order)

        #print("(min_rel_x, max_rel_x)", (min_rel_x, max_rel_x))
        #print("(min_rel_y, max_rel_y)", (min_rel_y, max_rel_y))
        return d_eta
        
        
    def _bicubic_interpolation_inner(self, coarse_eta, coarse_i, coarse_j, rel_x, rel_y, interpolation_order=3):
         # Matrix needed to find the interpolation coefficients
        bicubic_matrix = np.matrix([[ 1,  0,  0,  0], 
                                    [ 0,  0,  1,  0], 
                                    [-3,  3, -2, -1],
                                    [ 2, -2,  1,  1]])
        
        f00   =  coarse_eta[coarse_j  , coarse_i  ]
        f01   =  coarse_eta[coarse_j+1, coarse_i  ]
        f10   =  coarse_eta[coarse_j  , coarse_i+1]
        f11   =  coarse_eta[coarse_j+1, coarse_i+1]

        fx00  = (coarse_eta[coarse_j  , coarse_i+1] - coarse_eta[coarse_j  , coarse_i-1])/2
        fx01  = (coarse_eta[coarse_j+1, coarse_i+1] - coarse_eta[coarse_j+1, coarse_i-1])/2       
        fx10  = (coarse_eta[coarse_j  , coarse_i+2] - coarse_eta[coarse_j  , coarse_i  ])/2    
        fx11  = (coarse_eta[coarse_j+1, coarse_i+2] - coarse_eta[coarse_j+1, coarse_i  ])/2      

        fy00  = (coarse_eta[coarse_j+1, coarse_i  ] - coarse_eta[coarse_j-1, coarse_i  ])/2
        fy01  = (coarse_eta[coarse_j+2, coarse_i  ] - coarse_eta[coarse_j  , coarse_i  ])/2       
        fy10  = (coarse_eta[coarse_j+1, coarse_i+1] - coarse_eta[coarse_j-1, coarse_i+1])/2       
        fy11  = (coarse_eta[coarse_j+2, coarse_i+1] - coarse_eta[coarse_j  , coarse_i+1])/2       

        fy_10 = (coarse_eta[coarse_j+1, coarse_i-1] - coarse_eta[coarse_j-1, coarse_i-1])/2
        fy_11 = (coarse_eta[coarse_j+2, coarse_i-1] - coarse_eta[coarse_j  , coarse_i-1])/2
        fy20  = (coarse_eta[coarse_j+1, coarse_i+2] - coarse_eta[coarse_j-1, coarse_i+2])/2
        fy21  = (coarse_eta[coarse_j+2, coarse_i+2] - coarse_eta[coarse_j  , coarse_i+2])/2

        fxy00 = (fy10 - fy_10)/2
        fxy01 = (fy11 - fy_11)/2
        fxy10 = (fy20 -  fy00)/2
        fxy11 = (fy21 -  fy01)/2


        f_matrix = np.matrix([[ f00,  f01,  fy00,  fy01],
                              [ f10,  f11,  fy10,  fy11],
                              [fx00, fx01, fxy00, fxy01],
                              [fx10, fx11, fxy10, fxy11] ])

        a_matrix = np.dot(bicubic_matrix, np.dot(f_matrix, bicubic_matrix.transpose()))
        
        x_vec = np.matrix([1.0, rel_x, rel_x*rel_x, rel_x*rel_x*rel_x])
        y_vec = np.matrix([1.0, rel_y, rel_y*rel_y, rel_y*rel_y*rel_y]).transpose()

        if interpolation_order == 0:
            # Flat average:
            return 0.25*(f00 + f01 + f10 + f11)

        elif interpolation_order == 1:
            # Linear interpolation:
            return f00*(1-rel_x)*(1-rel_y) + f10*rel_x*(1-rel_y) + f01*(1-rel_x)*rel_y + f11*rel_x*rel_y

        elif interpolation_order == 3:
            # Bicubic interpolation (make sure that we return a float)
            return np.dot(x_vec, np.dot(a_matrix, y_vec))[0, 0]

