import os, sys
import math
import itertools

import numpy as np
import opengen as og

from obstacle_scanner.mmc_dynamic_obstacles import ObstacleScanner ### choose the right scanner
from mpc.mpc_generator import MpcModule

'''
File info:
    Ref     - [Trajectory generation for mobile robotsin a dynamic environment using nonlinear model predictive control, CASE2021]
            - [https://github.com/wljungbergh/mpc-trajectory-generator]
    Exe     - [No]
File description:
    Generate the trajectory by generating/using the defined MPC solver. 
File content:
    TrajectoryGenerator <class> - Build and run the MPC problem. Calculate the trajectory step by step.
Comments:
                                                                                      V [MPC] V
    [GPP] --global path & static obstacles--> [LPP] --refernece path & tube width--> [TG(Config)] <--dynamic obstacles-- [OS]
'''

class TrajectoryGenerator:
    '''
    Description:
        Generate a smooth trajectory based on the reference path and obstacle information.
        Use a configuration specified by 'utils/config'
    Arguments:
        config  <dotdict> - A dictionary in the dot form contains all information/parameters needed.
        build   <bool>    - If true, build the MPC module.
        verbose <bool>    - If true, show verbose.
    Attributes:
        __prtname     <str>     - The name to print while running this class.
        config        <dotdict> - As above mentioned.
        scanner       <object>  - The obstacle scanner offering the dynamic obstacle info.
        mpc_generator <object>  - The MPC module.
    Functions
        run               <run>  - Run.
    Comments:
        Have fun but may need to modify the dynamic obstacle part (search NOTE).
    '''
    def __init__(self, config, build=False, verbose=False):
        self.__prtname = '[Traj]'
        self.config = config
        self.vb = verbose
        
        self.scanner       = ObstacleScanner()
        self.mpc_generator = MpcModule(self.config)

        if build:
            self.mpc_generator.build()
        
        self.import_solver()

    def import_solver(self, root_dir=''):
        sys.path.append(os.path.join(root_dir, self.config.build_directory, self.config.optimizer_name))
        built_solver = __import__(self.config.optimizer_name)
        self.solver = built_solver.solver()

    def run(self, ref_path:list, start:list, end:list):
        '''
        Description:
            Run the trajectory planner.
        Arguments:
            ref_path  <list of tuple> - Reference path
            start     <list> - The start state [x, y, theta]
            end       <list> - The end state [x, y, theta]
        Return:
            xx             <list> - List of x coordinate.
            xy             <list> - List of y coordinate.
            uv             <list> - List of velocity input.
            uomega         <list> - List of angular velocity input.
        Comments:
            'x_init'    <list>          [x, y, theta]
            'x_finish'  <list>          [x, y, theta]
        '''
        # Initialize tuning parameters to be passed to solver
        parameter_list = [self.config.qp, self.config.qv, self.config.qtheta, self.config.lin_vel_penalty, self.config.ang_vel_penalty,
                          self.config.qpN, self.config.qthetaN, self.config.qcte, self.config.lin_acc_penalty, self.config.ang_acc_penalty]
        # generate costs to establish initial heading
        p_init_c = [0.0*i for i in parameter_list]
        p_init_c[2] = np.max(parameter_list)

        x_ref, y_ref, theta_ref = self.mpc_generator.gen_ref_trajectory( (start[0], start[1]), ref_path[1:] )

        ### Prepare for the loop computing ###
        N_hor               = self.config.N_hor # frequently used: control/prediction horizon
        params_per_dyn_obs  = N_hor*self.config.ndynobs
        base_speed          = self.config.lin_vel_max*self.config.throttle_ratio
        
        system_input = [] # initalize list with selected system inputs/velocities
        states = start.copy() # initialiize states as starting state
        ref_points = [(x, y) for x, y in zip(x_ref, y_ref)]

        # Initialize lists
        refs = [0.0] * (N_hor * self.config.ns)
        # stc_constraints = [0.0] * self.config.Nobs*self.config.nobs
        dyn_constraints = [0.0] * self.config.Ndynobs * self.config.ndynobs*N_hor
        # Avoid dividing by zero in MPC solver by init x radius and y radius to 1
        dyn_constraints[2::self.config.ndynobs] = [1.0] * self.config.Ndynobs*N_hor
        dyn_constraints[3::self.config.ndynobs] = [1.0] * self.config.Ndynobs*N_hor

        ### Start the loop ###
        t = 0   # time step, start from 0
        idx = 0 # index of the current reference trajectory point
        terminal = False
        establish_heading = False
    
        while (not terminal) and t < 500.0/self.config.ts:
            x_init = states[-self.config.ns:] # set current state as initial state for solver

            if t == 0: # NOTE May vary for different types of obstacles
                full_obstacle_list = self.scanner.get_full_obstacle_list(current_time=(t*self.config.ts), horizon=N_hor, ts=self.config.ts)
                for i, dyn_obstacle in enumerate(full_obstacle_list):
                    dyn_constraints[i*params_per_dyn_obs:(i+1)*params_per_dyn_obs] = list(itertools.chain(*dyn_obstacle))
            else: # Rotate list to the left
                dyn_constraints = dyn_constraints[self.config.ndynobs*self.config.num_steps_taken:] + \
                                    dyn_constraints[:self.config.ndynobs*self.config.num_steps_taken]
                current_time = (t+N_hor-self.config.num_steps_taken)*self.config.ts
                full_obstacle_list = self.scanner.get_full_obstacle_list(current_time=current_time, horizon=self.config.num_steps_taken, ts=self.config.ts)
                for i, dyn_obstacle in enumerate(full_obstacle_list):
                    # Update last num_steps taken dynobs positions
                    dyn_constraints[(i+1)*params_per_dyn_obs-self.config.ndynobs*self.config.num_steps_taken:(i+1)*params_per_dyn_obs] = list(
                        itertools.chain(*dyn_obstacle))

            ### Get reference states ###
            lb_idx = max(0, idx-1*self.config.num_steps_taken)                  # reduce search space for closest reference point
            ub_idx = min(len(ref_points), idx+5*self.config.num_steps_taken)    # reduce search space for closest reference point

            distances = [math.hypot(x_init[0]-x[0], x_init[1]-x[1]) for x in ref_points[lb_idx:ub_idx]]
            idx = distances.index(min(distances))

            idx += lb_idx  # idx in orignal reference trajectory list
            if (idx+N_hor >= len(x_ref)):
                x_finish = end
                tmpx = x_ref[idx:]      + [end[0]]*(N_hor-(len(x_ref)-idx))
                tmpy = y_ref[idx:]      + [end[1]]*(N_hor-(len(y_ref)-idx))
                tmpt = theta_ref[idx:]  + [end[2]]*(N_hor-(len(theta_ref)-idx))
            else:
                x_finish = [x_ref[idx+N_hor], y_ref[idx+N_hor], theta_ref[idx+N_hor]]
                tmpx = x_ref[idx:idx+N_hor]
                tmpy = y_ref[idx:idx+N_hor]
                tmpt = theta_ref[idx:idx+N_hor]
            refs[0::self.config.ns] = tmpx
            refs[1::self.config.ns] = tmpy
            refs[2::self.config.ns] = tmpt

            ### Get reference velocities ###
            vel_ref = [base_speed]*self.config.N_hor

            if establish_heading and abs(theta_ref[idx]-states[-1])<(math.pi/6): # if initial heading established
                establish_heading = False

            if len(system_input):
                last_u = system_input[-self.config.nu:]
            else:
                last_u = [0.0] * self.config.nu

            ### Assemble parameters for solver ###
            if(not establish_heading):
                params = x_init + last_u + x_finish + \
                        parameter_list + \
                        refs + vel_ref + \
                        dyn_constraints
            else:
                params = x_init + last_u + x_finish + \
                        p_init_c + \
                        refs + vel_ref + \
                        dyn_constraints

            try:
                exit_status, solver_time = self.mpc_generator.run(params, self.solver, self.config.num_steps_taken, system_input, states)
            except RuntimeError as err:
                print("Fatal: Cannot run.")
                if self.vb:
                    print(err)
                return

            if exit_status in self.config.bad_exit_codes and self.vb:
                print(f"{self.__prtname} Bad converge status: {exit_status}")

            if np.allclose(states[-3:-1], end[0:2], atol=0.05, rtol=0) and abs(system_input[-2]) < 0.005:
                terminal = True
                print(f"{self.__prtname} MPC solution found.")

            t += self.config.num_steps_taken

        xx = states[0:len(states):self.config.ns]
        xy = states[1:len(states):self.config.ns]
        uv = system_input[0:len(system_input):2]
        uomega = system_input[1:len(system_input):2]

        return xx, xy, uv, uomega
