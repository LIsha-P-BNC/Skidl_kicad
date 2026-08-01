from collections import defaultdict
from skidl import Pin, Part, Alias, SchLib, SKIDL, TEMPLATE

from skidl.pin import pin_types

SKIDL_lib_version = '0.0.1'

atmega32u2_usb_test = SchLib(tool=SKIDL).add_parts(*[
        Part(**{ 'name':'ATmega32U2-A', 'dest':TEMPLATE, 'tool':SKIDL, 'aliases':Alias({'ATmega32U2-A'}), 'ref_prefix':'U', 'fplist':['Package_QFP:TQFP-32_7x7mm_P0.8mm', 'Package_QFP:TQFP-32_7x7mm_P0.8mm'], 'footprint':'Package_QFP:TQFP-32_7x7mm_P0.8mm', 'keywords':'AVR 8bit Microcontroller MegaAVR', 'description':'16MHz, 32kB Flash, 1kB SRAM, 1kB EEPROM, TQFP-32', 'datasheet':'http://ww1.microchip.com/downloads/en/DeviceDoc/doc7799.pdf', 'pins':[
            Pin(num='1',name='XTAL1',func=pin_types.INPUT,unit=1),
            Pin(num='2',name='PC0/XTAL2',func=pin_types.BIDIR,unit=1),
            Pin(num='3',name='GND',func=pin_types.PWRIN,unit=1),
            Pin(num='4',name='VCC',func=pin_types.PWRIN,unit=1),
            Pin(num='5',name='PC2',func=pin_types.BIDIR,unit=1),
            Pin(num='6',name='PD0',func=pin_types.BIDIR,unit=1),
            Pin(num='7',name='PD1',func=pin_types.BIDIR,unit=1),
            Pin(num='8',name='PD2',func=pin_types.BIDIR,unit=1),
            Pin(num='9',name='PD3',func=pin_types.BIDIR,unit=1),
            Pin(num='10',name='PD4',func=pin_types.BIDIR,unit=1),
            Pin(num='11',name='PD5',func=pin_types.BIDIR,unit=1),
            Pin(num='12',name='PD6',func=pin_types.BIDIR,unit=1),
            Pin(num='13',name='~{HWB}/PD7',func=pin_types.BIDIR,unit=1),
            Pin(num='14',name='PB0',func=pin_types.BIDIR,unit=1),
            Pin(num='15',name='PB1',func=pin_types.BIDIR,unit=1),
            Pin(num='16',name='PB2',func=pin_types.BIDIR,unit=1),
            Pin(num='17',name='PB3',func=pin_types.BIDIR,unit=1),
            Pin(num='18',name='PB4',func=pin_types.BIDIR,unit=1),
            Pin(num='19',name='PB5',func=pin_types.BIDIR,unit=1),
            Pin(num='20',name='PB6',func=pin_types.BIDIR,unit=1),
            Pin(num='21',name='PB7',func=pin_types.BIDIR,unit=1),
            Pin(num='22',name='PC7',func=pin_types.BIDIR,unit=1),
            Pin(num='23',name='PC6',func=pin_types.BIDIR,unit=1),
            Pin(num='24',name='PC1/~{RESET}',func=pin_types.BIDIR,unit=1),
            Pin(num='25',name='PC5',func=pin_types.BIDIR,unit=1),
            Pin(num='26',name='PC4',func=pin_types.BIDIR,unit=1),
            Pin(num='27',name='UCAP',func=pin_types.PASSIVE,unit=1),
            Pin(num='28',name='UGND',func=pin_types.PWRIN,unit=1),
            Pin(num='29',name='D+',func=pin_types.BIDIR,unit=1),
            Pin(num='30',name='D-',func=pin_types.BIDIR,unit=1),
            Pin(num='31',name='UVCC',func=pin_types.PWRIN,unit=1),
            Pin(num='32',name='AVCC',func=pin_types.PWRIN,unit=1)], 'unit_defs':[] }),
        Part(**{ 'name':'USB_B', 'dest':TEMPLATE, 'tool':SKIDL, 'aliases':Alias({'USB_B'}), 'ref_prefix':'J', 'fplist':['Connector_USB:USB_A_Connfly_DS1095'], 'footprint':'Connector_USB:USB_B_Molex_67068', 'keywords':'connector USB', 'description':'USB Type B connector', 'datasheet':'', 'pins':[
            Pin(num='1',name='VBUS',func=pin_types.PWROUT,unit=1),
            Pin(num='2',name='D-',func=pin_types.BIDIR,unit=1),
            Pin(num='3',name='D+',func=pin_types.BIDIR,unit=1),
            Pin(num='4',name='GND',func=pin_types.PWROUT,unit=1),
            Pin(num='SH',name='Shield',func=pin_types.PASSIVE,unit=1)], 'unit_defs':[] }),
        Part(**{ 'name':'R', 'dest':TEMPLATE, 'tool':SKIDL, 'aliases':Alias({'R'}), 'ref_prefix':'R', 'fplist':['Resistor_SMD:R_Cat16-2'], 'footprint':'Resistor_SMD:R_0603_1608Metric', 'keywords':'R res resistor', 'description':'Resistor', 'datasheet':'', 'pins':[
            Pin(num='1',func=pin_types.PASSIVE,unit=1),
            Pin(num='2',func=pin_types.PASSIVE,unit=1)], 'unit_defs':[] }),
        Part(**{ 'name':'C', 'dest':TEMPLATE, 'tool':SKIDL, 'aliases':Alias({'C'}), 'ref_prefix':'C', 'fplist':['Capacitor_SMD:C_Elec_3x5.4'], 'footprint':'Capacitor_SMD:C_0805_2012Metric', 'keywords':'cap capacitor', 'description':'Unpolarized capacitor', 'datasheet':'', 'pins':[
            Pin(num='1',func=pin_types.PASSIVE,unit=1),
            Pin(num='2',func=pin_types.PASSIVE,unit=1)], 'unit_defs':[] }),
        Part(**{ 'name':'Crystal', 'dest':TEMPLATE, 'tool':SKIDL, 'aliases':Alias({'Crystal'}), 'ref_prefix':'Y', 'fplist':['Crystal:Crystal_HC35-U'], 'footprint':'Crystal_SMD:Crystal_SMD_3225-4Pin_3.2x2.5mm', 'keywords':'quartz ceramic resonator oscillator', 'description':'Two pin crystal', 'datasheet':'', 'pins':[
            Pin(num='1',name='1',func=pin_types.PASSIVE,unit=1),
            Pin(num='2',name='2',func=pin_types.PASSIVE,unit=1)], 'unit_defs':[] }),
        Part(**{ 'name':'SW_SPST', 'dest':TEMPLATE, 'tool':SKIDL, 'aliases':Alias({'SW_SPST'}), 'ref_prefix':'SW', 'fplist':[''], 'footprint':'Button_Switch_THT:SW_PUSH_6mm_H5mm', 'keywords':'switch lever', 'description':'Single Pole Single Throw (SPST) switch', 'datasheet':'', 'pins':[
            Pin(num='1',name='A',func=pin_types.PASSIVE,unit=1),
            Pin(num='2',name='B',func=pin_types.PASSIVE,unit=1)], 'unit_defs':[] }),
        Part(**{ 'name':'LED', 'dest':TEMPLATE, 'tool':SKIDL, 'aliases':Alias({'LED'}), 'ref_prefix':'D', 'fplist':['LED_THT:LED_D3.0mm'], 'footprint':'LED_SMD:LED_0603_1608Metric', 'keywords':'LED diode', 'description':'Light emitting diode', 'datasheet':'', 'pins':[
            Pin(num='1',name='K',func=pin_types.PASSIVE,unit=1),
            Pin(num='2',name='A',func=pin_types.PASSIVE,unit=1)], 'unit_defs':[] }),
        Part(**{ 'name':'Conn_01x08', 'dest':TEMPLATE, 'tool':SKIDL, 'aliases':Alias({'Conn_01x08'}), 'ref_prefix':'J', 'fplist':[''], 'footprint':'Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical', 'keywords':'connector', 'description':'Generic connector, single row, 01x08, script generated (kicad-library-utils/schlib/autogen/connector/)', 'datasheet':'', 'pins':[
            Pin(num='1',name='Pin_1',func=pin_types.PASSIVE,unit=1),
            Pin(num='2',name='Pin_2',func=pin_types.PASSIVE,unit=1),
            Pin(num='3',name='Pin_3',func=pin_types.PASSIVE,unit=1),
            Pin(num='4',name='Pin_4',func=pin_types.PASSIVE,unit=1),
            Pin(num='5',name='Pin_5',func=pin_types.PASSIVE,unit=1),
            Pin(num='6',name='Pin_6',func=pin_types.PASSIVE,unit=1),
            Pin(num='7',name='Pin_7',func=pin_types.PASSIVE,unit=1),
            Pin(num='8',name='Pin_8',func=pin_types.PASSIVE,unit=1)], 'unit_defs':[] })])