"""
src/skidl/board/place/global_opt.py

Connectivity-driven GLOBAL block placement: blocks that share many
weighted nets (clock 8x, RF 10x, buses 3x, LEDs 0.3x -- see
net_classify.NET_PLACEMENT_WEIGHTS) attract each other; overlapping
blocks repel along the smallest separating axis. This is the
"every net is a spring" optimization professionals apply between
clustering and legalization.

DETERMINISTIC by construction: fixed iteration count, no randomness,
ties broken by block index -- identical inputs give identical layouts
(resume/cache safety). The schematic-domain force placer
(schematics/place.py) is deliberately NOT reused: it is random-seeded
and entangled with schematic symbol geometry.
"""

from __future__ import annotations

ITERATIONS = 200
MIN_MOVE = 0.05      # mm; stop early when the largest move drops below


def block_bbox(fps):
    from skidl.board.place.simple import _world_bbox
    boxes = [_world_bbox(fp) for fp in fps]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def optimize_blocks(blocks, affinity, fixed_boxes=(), outline_wh=None,
                    gap=1.0) -> dict:
    """Mutates footprint positions: each block (list of Footprints)
    moves as a rigid body. `affinity`: {(i, j): weight} from
    cluster.block_affinity_matrix. `fixed_boxes`: immovable world rects
    (mechanical keepouts/connectors) that repel blocks. `outline_wh`
    clamps block centers inside a fixed board. Returns a small report."""
    n = len(blocks)
    if n < 2:
        return {"iterations": 0, "moved": False}

    boxes = [block_bbox(b) for b in blocks]

    def center(box):
        return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)

    # Per-block attraction lists.
    partners = {i: [] for i in range(n)}
    for (i, j), w in sorted(affinity.items()):
        partners[i].append((j, w))
        partners[j].append((i, w))

    max_move_seen = 0.0
    for t in range(ITERATIONS):
        alpha = 0.5 * (1.0 - t / ITERATIONS)
        max_move = 0.0
        for i in range(n):
            plist = partners[i]
            fx = fy = 0.0
            if plist:
                wsum = sum(w for _, w in plist)
                cx, cy = center(boxes[i])
                for j, w in plist:
                    ox, oy = center(boxes[j])
                    fx += w * (ox - cx)
                    fy += w * (oy - cy)
                fx /= wsum
                fy /= wsum
            # Repulsion: smallest separating axis from overlapping rects.
            x1, y1, x2, y2 = boxes[i]
            for j in range(n):
                if j == i:
                    continue
                ox1, oy1, ox2, oy2 = boxes[j]
                dx = min(x2, ox2) - max(x1, ox1) + gap
                dy = min(y2, oy2) - max(y1, oy1) + gap
                if dx > 0 and dy > 0:
                    if dx <= dy:
                        fx += dx if (x1 + x2) >= (ox1 + ox2) else -dx
                    else:
                        fy += dy if (y1 + y2) >= (oy1 + oy2) else -dy
            for (ox1, oy1, ox2, oy2) in fixed_boxes:
                dx = min(x2, ox2) - max(x1, ox1) + gap
                dy = min(y2, oy2) - max(y1, oy1) + gap
                if dx > 0 and dy > 0:
                    if dx <= dy:
                        fx += dx if (x1 + x2) >= (ox1 + ox2) else -dx
                    else:
                        fy += dy if (y1 + y2) >= (oy1 + oy2) else -dy
            mx, my = alpha * fx, alpha * fy
            if outline_wh:
                W, H = outline_wh
                w2, h2 = (x2 - x1), (y2 - y1)
                nx1 = min(max(x1 + mx, 0.0), max(0.0, W - w2))
                ny1 = min(max(y1 + my, 0.0), max(0.0, H - h2))
                mx, my = nx1 - x1, ny1 - y1
            if mx or my:
                for fp in blocks[i]:
                    fp.x += mx
                    fp.y += my
                boxes[i] = (x1 + mx, y1 + my, x2 + mx, y2 + my)
                max_move = max(max_move, abs(mx), abs(my))
        max_move_seen = max(max_move_seen, max_move)
        if max_move < MIN_MOVE:
            break
    return {"iterations": t + 1, "moved": max_move_seen > 0.0}
