# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

from skidl.geometry import BBox, Point, Segment
from skidl.schematics.metrics import (
    component_density,
    label_count,
    readability_score,
    total_wire_length,
    whitespace_ratio,
)


class FakePin:
    def __init__(self, connected=True):
        self._connected = connected

    def is_connected(self):
        return self._connected


class FakeNet:
    def __init__(self, stub, pins):
        self.stub = stub
        self.pins = pins


def test_total_wire_length_sums_manhattan_lengths():
    wires = {
        "net_a": [Segment(Point(0, 0), Point(10, 0)), Segment(Point(10, 0), Point(10, 5))],
        "net_b": [Segment(Point(0, 0), Point(0, 3))],
    }
    assert total_wire_length(wires) == 10 + 5 + 3


def test_total_wire_length_empty():
    assert total_wire_length({}) == 0.0


def test_label_count_only_counts_stubbed_connected_pins():
    stubbed_net = FakeNet(stub=True, pins=[FakePin(True), FakePin(True), FakePin(False)])
    plain_net = FakeNet(stub=False, pins=[FakePin(True), FakePin(True)])
    assert label_count([stubbed_net, plain_net]) == 2


def test_label_count_no_stubbed_nets():
    plain_net = FakeNet(stub=False, pins=[FakePin(True)])
    assert label_count([plain_net]) == 0


def test_component_density():
    node_bbox = BBox(Point(0, 0), Point(10, 10))  # area 100
    part_bboxes = [BBox(Point(0, 0), Point(1, 1)) for _ in range(4)]
    assert component_density(part_bboxes, node_bbox) == 4 / 100


def test_component_density_zero_area_node():
    node_bbox = BBox(Point(0, 0), Point(0, 0))
    assert component_density([], node_bbox) == 0.0


def test_whitespace_ratio_half_covered():
    node_bbox = BBox(Point(0, 0), Point(10, 10))  # area 100
    part_bboxes = [BBox(Point(0, 0), Point(10, 5))]  # area 50
    assert whitespace_ratio(part_bboxes, node_bbox) == 0.5


def test_whitespace_ratio_fully_covered_clamped():
    node_bbox = BBox(Point(0, 0), Point(10, 10))
    part_bboxes = [BBox(Point(0, 0), Point(10, 10)), BBox(Point(0, 0), Point(10, 10))]
    assert whitespace_ratio(part_bboxes, node_bbox) == 0.0


def test_whitespace_ratio_zero_area_node():
    node_bbox = BBox(Point(0, 0), Point(0, 0))
    assert whitespace_ratio([], node_bbox) == 0.0


def test_readability_score_combines_metrics_and_omits_crossings_by_default():
    node_bbox = BBox(Point(0, 0), Point(10, 10))
    wires = {"net_a": [Segment(Point(0, 0), Point(10, 0))]}
    nets = [FakeNet(stub=True, pins=[FakePin(True)])]
    part_bboxes = [BBox(Point(0, 0), Point(1, 1))]

    result = readability_score(wires, nets, part_bboxes, node_bbox)

    assert result["wire_length"] == 10
    assert result["labels"] == 1
    assert result["crossings"] == 0
    assert result["score"] > 0


def test_readability_score_lower_is_better_when_crossings_added():
    node_bbox = BBox(Point(0, 0), Point(10, 10))
    wires = {}
    nets = []
    part_bboxes = []

    baseline = readability_score(wires, nets, part_bboxes, node_bbox, crossings=0)
    with_crossings = readability_score(wires, nets, part_bboxes, node_bbox, crossings=3)

    assert with_crossings["score"] > baseline["score"]


def test_readability_score_custom_weights():
    node_bbox = BBox(Point(0, 0), Point(10, 10))
    wires = {"net_a": [Segment(Point(0, 0), Point(10, 0))]}

    result = readability_score(
        wires,
        [],
        [],
        node_bbox,
        weights={
            "wire_length": 5.0,
            "labels": 0.0,
            "density": 0.0,
            "whitespace": 0.0,
            "crossings": 0.0,
        },
    )
    assert result["score"] == 5.0 * 10
