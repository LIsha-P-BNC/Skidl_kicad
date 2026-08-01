# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

import pytest

from skidl import ERC, SKIDL, TEMPLATE, Circuit, Part, Pin, erc_logger, subcircuit
from skidl.pin import pin_types


def test_NC_1():
    """Test the NC (No Connect) functionality."""
    
    @subcircuit
    def circ_nc():
        """Create a subcircuit with a resistor."""
        res = Part(
            tool=SKIDL,
            name="res",
            ref_prefix="R",
            dest=TEMPLATE,
            pins=[
                Pin(num=1, func=pin_types.PASSIVE),  # Define pin 1 as passive
                Pin(num=2, func=pin_types.PASSIVE),  # Define pin 2 as passive
            ],
        )
        r1 = res()  # Instantiate the resistor part
        r1[1] += NC  # Connect pin 1 to NC (No Connect)

    default_circuit.name = "DEFAULT"  # Set the name of the default circuit
    circuit1 = Circuit(name="CIRCUIT1")  # Create a new circuit named CIRCUIT1
    circ_nc()  # Add the subcircuit to the default circuit
    circ_nc(circuit=circuit1)  # Add the subcircuit to circuit1


def test_NC_power_pin_warns():
    """Connecting a power pin to NC should be flagged, not silently accepted."""

    reg = Part(
        tool=SKIDL,
        name="reg",
        ref_prefix="U",
        dest=TEMPLATE,
        pins=[
            Pin(num=1, name="VDD", func=pin_types.PWRIN),
            Pin(num=2, name="OUT", func=pin_types.OUTPUT),
        ],
    )
    u1 = reg()
    u1["VDD"] += NC  # Mistake: tying a power pin to no-connect.
    u1["OUT"] += NC  # Unrelated NC use, kept connected so it doesn't also
    # trip the pre-existing "unconnected pin" warning and muddy the count.

    ERC()
    assert erc_logger.warning.count == 1
    assert erc_logger.error.count == 0


def test_NC_non_power_pin_no_warning():
    """A non-power pin tied to NC is normal usage and shouldn't warn."""

    reg = Part(
        tool=SKIDL,
        name="reg",
        ref_prefix="U",
        dest=TEMPLATE,
        pins=[
            Pin(num=1, name="VDD", func=pin_types.PWRIN),
            Pin(num=2, name="VOUT", func=pin_types.PWROUT),
            Pin(num=3, name="NC_PIN", func=pin_types.OUTPUT),
        ],
    )
    u1 = reg()
    u1["VDD"] += u1["VOUT"]  # Paired so net-driver checks are satisfied.
    u1["NC_PIN"] += NC  # Ordinary unused pin tied off intentionally.

    ERC()
    assert erc_logger.warning.count == 0
    assert erc_logger.error.count == 0
