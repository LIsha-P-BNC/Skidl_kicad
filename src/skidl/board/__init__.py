"""Pure-python PCB board pipeline (BoardModel/BoardBackend). See the M0/M1
spec for the full A-E pipeline this package implements piece by piece;
this package currently covers M0 ([A] pcb_writer) plus the BoardBackend
seam that M1 (routing) and M2 (real placement) plug into."""

from skidl.board.model import BoardModel, Footprint, Pad, Net, Track, Via

__all__ = ["BoardModel", "Footprint", "Pad", "Net", "Track", "Via"]
