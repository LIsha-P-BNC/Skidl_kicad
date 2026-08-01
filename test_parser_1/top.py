# -*- coding: utf-8 -*-
from skidl import *
from sub1_1 import sub1_1
from sub1_2 import sub1_2

@subcircuit
def top():
    # Local nets
    N_1 = Net('N$1')
    N_2 = Net('N$2')

    # Hierarchical subcircuits
    sub1_1(N_1, N_2)
    sub1_2(N_1, N_2)
    return
