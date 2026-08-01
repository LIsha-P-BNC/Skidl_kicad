# -*- coding: utf-8 -*-
from skidl import *

@subcircuit
def sub1_1(N_1, N_2):
    # Local nets
    N_5 = Net('N$5')

    # Components
    C1 = Part('Device', 'C', value='0.001', ref='C1')
    C2 = Part('Device', 'C', value='0.002', ref='C2')
    R1 = Part('Device', 'R', value='0.001', ref='R1')
    R2 = Part('Device', 'R', value='0.002', ref='R2')


    # Connections
    N_1 += C1['1'], R1['1']
    N_2 += C2['2'], R2['2']
    N_5 += C1['2'], C2['1'], R1['2'], R2['1']
    return
