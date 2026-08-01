# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

from skidl.geometry import Point, Segment


def test_intersects_crossing():
    """Two segments that cross in an X should intersect."""
    a = Segment(Point(0, 0), Point(10, 10))
    b = Segment(Point(0, 10), Point(10, 0))
    assert a.intersects(b)
    assert b.intersects(a)


def test_intersects_non_crossing():
    """Two segments with no common point should not intersect."""
    a = Segment(Point(0, 0), Point(1, 1))
    b = Segment(Point(5, 5), Point(6, 6))
    assert not a.intersects(b)


def test_intersects_perpendicular_manhattan():
    """A horizontal and vertical segment crossing like a Manhattan wire junction."""
    h = Segment(Point(0, 5), Point(10, 5))
    v = Segment(Point(5, 0), Point(5, 10))
    assert h.intersects(v)


def test_intersects_perpendicular_manhattan_miss():
    """A horizontal and vertical segment that don't reach each other."""
    h = Segment(Point(0, 5), Point(4, 5))
    v = Segment(Point(5, 0), Point(5, 10))
    assert not h.intersects(v)


def test_intersects_touching_endpoint():
    """Segments sharing an endpoint (a T-touch) count as intersecting."""
    a = Segment(Point(0, 0), Point(5, 5))
    b = Segment(Point(5, 5), Point(10, 0))
    assert a.intersects(b)


def test_intersects_parallel_no_overlap():
    """Parallel, non-collinear segments never intersect."""
    a = Segment(Point(0, 0), Point(10, 0))
    b = Segment(Point(0, 5), Point(10, 5))
    assert not a.intersects(b)


def test_intersects_collinear_overlap_not_a_crossing():
    """Collinear overlapping segments have a zero denominator and are
    reported as non-intersecting by intersects() -- that case is what
    shadows() is for, not a visual X crossing."""
    a = Segment(Point(0, 0), Point(10, 0))
    b = Segment(Point(5, 0), Point(15, 0))
    assert not a.intersects(b)


def test_shadows_horizontal_overlap():
    a = Segment(Point(3, 0), Point(3, 10))
    b = Segment(Point(3, 5), Point(3, 15))
    assert a.shadows(b)


def test_shadows_horizontal_no_overlap():
    a = Segment(Point(3, 0), Point(3, 10))
    b = Segment(Point(3, 20), Point(3, 30))
    assert not a.shadows(b)


def test_shadows_vertical_overlap():
    a = Segment(Point(0, 3), Point(10, 3))
    b = Segment(Point(5, 3), Point(15, 3))
    assert a.shadows(b)


def test_shadows_non_axis_aligned_never_shadows():
    a = Segment(Point(0, 0), Point(10, 10))
    b = Segment(Point(0, 0), Point(10, 10))
    assert not a.shadows(b)
