"""board/geom.py -- pure geometry helpers used by the pour planner."""
from skidl.board.geom import (
    point_in_polygon, bbox_of, bbox_contains, bboxes_overlap,
)

SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]
# concave "L" shape
L = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)]


def test_point_inside_square():
    assert point_in_polygon((5, 5), SQUARE)


def test_point_outside_square():
    assert not point_in_polygon((15, 5), SQUARE)
    assert not point_in_polygon((-1, 5), SQUARE)


def test_point_on_edge_is_inside():
    assert point_in_polygon((0, 5), SQUARE)
    assert point_in_polygon((10, 10), SQUARE)


def test_concave_polygon_notch_excluded():
    assert point_in_polygon((2, 2), L)        # in the solid arm
    assert not point_in_polygon((7, 7), L)     # in the cut-out notch


def test_degenerate_polygon_is_outside():
    assert not point_in_polygon((0, 0), [])
    assert not point_in_polygon((0, 0), [(0, 0), (1, 1)])


def test_bbox_helpers():
    assert bbox_of(SQUARE) == (0, 0, 10, 10)
    assert bbox_of([]) is None
    assert bbox_contains((0, 0, 10, 10), (5, 5))
    assert not bbox_contains((0, 0, 10, 10), (11, 5))
    assert bbox_contains((0, 0, 10, 10), (11, 5), margin=1.0)


def test_bbox_overlap():
    assert bboxes_overlap((0, 0, 5, 5), (4, 4, 9, 9))
    assert not bboxes_overlap((0, 0, 5, 5), (6, 6, 9, 9))
    # touching within gap counts as overlap
    assert bboxes_overlap((0, 0, 5, 5), (6, 0, 9, 5), gap=1.5)
