# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

"""
Objective readability metrics for generated schematics.

These are diagnostic/regression-tracking functions, not rendering features:
they measure wire length, label density, part density, whitespace, and
(once available) wire crossings so placement/routing quality changes can be
validated against a fixed test corpus instead of relying on eyeballing
screenshots. Each function takes plain data (segments, bboxes, nets) rather
than a live SchNode so it can be unit-tested with small hand-built fixtures.
"""

from skidl.utilities import export_to_all


@export_to_all
def total_wire_length(wires):
    """
    Sum the Manhattan length of every routed wire segment.

    Args:
        wires (dict): Mapping of net -> list of Segment, e.g. SchNode.wires.

    Returns:
        float: Total length of all wire segments.
    """
    length = 0.0
    for segments in wires.values():
        for seg in segments:
            length += abs(seg.p2.x - seg.p1.x) + abs(seg.p2.y - seg.p1.y)
    return length


@export_to_all
def label_count(nets):
    """
    Count the number of net-label glyphs a set of nets will render as.

    A stubbed net emits one label per connected pin on it (mirroring how
    net_label_to_sexp() in sexp_schematic.py is called once per stubbed,
    connected pin), so this is a glyph count, not a net count.

    Args:
        nets (iterable): Net objects to inspect.

    Returns:
        int: Total label instance count.
    """
    count = 0
    for net in nets:
        if getattr(net, "stub", False):
            count += sum(1 for pin in net.pins if pin.is_connected())
    return count


@export_to_all
def component_density(part_bboxes, node_bbox):
    """
    Return parts per unit area within a node's bounding box.

    Args:
        part_bboxes (list): BBox objects for each placed part.
        node_bbox (BBox): The overall node bounding box.

    Returns:
        float: Number of parts per unit area (0 if node_bbox has no area).
    """
    area = node_bbox.area
    if area <= 0:
        return 0.0
    return len(part_bboxes) / area


@export_to_all
def whitespace_ratio(part_bboxes, node_bbox):
    """
    Return the fraction of a node's bounding box not covered by any part.

    Args:
        part_bboxes (list): BBox objects for each placed part.
        node_bbox (BBox): The overall node bounding box.

    Returns:
        float: 1.0 = fully empty, 0.0 = fully covered (clamped to [0,1]).
    """
    total_area = node_bbox.area
    if total_area <= 0:
        return 0.0
    covered = sum(bbox.area for bbox in part_bboxes)
    return max(0.0, min(1.0, 1.0 - (covered / total_area)))


@export_to_all
def readability_score(
    wires,
    nets,
    part_bboxes,
    node_bbox,
    crossings=None,
    weights=None,
):
    """
    Combine the individual metrics into a single readability score.

    Lower is better. This is a regression-tracking aid (compare a score
    before/after a placement or routing change on the same circuit), not
    an absolute quality target -- the weights are a starting point and are
    expected to be tuned against visual QA over time.

    Args:
        wires (dict): Mapping of net -> list of Segment, e.g. SchNode.wires.
        nets (iterable): Net objects to inspect for label density.
        part_bboxes (list): BBox objects for each placed part.
        node_bbox (BBox): The overall node bounding box.
        crossings (int, optional): Wire-crossing count from
            route.count_wire_crossings(). Omitted (None) until that
            function is available; contributes 0 to the score if omitted.
        weights (dict, optional): Override weights for any of
            "wire_length", "labels", "density", "whitespace", "crossings".

    Returns:
        dict: The individual metric values plus the combined "score".
    """
    default_weights = {
        "wire_length": 1.0,
        "labels": 50.0,
        "density": 1.0,
        "whitespace": 100.0,
        "crossings": 200.0,
    }
    if weights:
        default_weights.update(weights)

    metrics = {
        "wire_length": total_wire_length(wires),
        "labels": label_count(nets),
        "density": component_density(part_bboxes, node_bbox),
        "whitespace": whitespace_ratio(part_bboxes, node_bbox),
        "crossings": crossings if crossings is not None else 0,
    }

    score = sum(default_weights[key] * value for key, value in metrics.items())
    metrics["score"] = score
    return metrics
