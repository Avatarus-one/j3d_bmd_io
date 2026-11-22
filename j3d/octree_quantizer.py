"""Octree color quantizer used for GX palette textures (C4 / C8).

Based on delimitry/octree_color_quantizer, ported to Python 3 and extended
with an alpha channel (the fourth octree axis) so RGBA images can be
quantised to a TLUT palette.

MIT License

Copyright (c) 2016 Dmitry Alimov

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


class Color:
    """RGBA color used by the quantizer."""

    __slots__ = ("red", "green", "blue", "alpha")

    def __init__(self, red=0, green=0, blue=0, alpha=255):
        self.red = red
        self.green = green
        self.blue = blue
        self.alpha = alpha


class OctreeNode:
    """Octree node for color quantization.

    RGB is extended with alpha, so every level uses four bits (16 children).
    """

    def __init__(self, level, parent):
        self.color = Color(0, 0, 0, 0)
        self.pixel_count = 0
        self.palette_index = 0
        self.children = [None for _ in range(16)]
        # add node to current level
        if level < OctreeQuantizer.MAX_DEPTH - 1:
            parent.add_level_node(level, self)

    def is_leaf(self):
        """Check that node is leaf."""
        return self.pixel_count > 0

    def get_leaf_nodes(self):
        """Get all leaf nodes."""
        leaf_nodes = []
        for node in self.children:
            if node:
                if node.is_leaf():
                    leaf_nodes.append(node)
                else:
                    leaf_nodes.extend(node.get_leaf_nodes())
        return leaf_nodes

    def add_color(self, color, level, parent, count=1):
        """Add *color* (weighted by *count*) to the tree."""
        if level >= OctreeQuantizer.MAX_DEPTH:
            self.color.red += color.red * count
            self.color.green += color.green * count
            self.color.blue += color.blue * count
            self.color.alpha += color.alpha * count
            self.pixel_count += count
            return
        index = self.get_color_index_for_level(color, level)
        if not self.children[index]:
            self.children[index] = OctreeNode(level, parent)
        self.children[index].add_color(color, level + 1, parent, count)

    def get_palette_index(self, color, level):
        """Get the palette index for *color*; use *level* to go deeper."""
        if self.is_leaf():
            return self.palette_index
        index = self.get_color_index_for_level(color, level)
        if self.children[index]:
            return self.children[index].get_palette_index(color, level + 1)
        # get palette index for a first found child node
        for node in self.children:
            if node:
                return node.get_palette_index(color, level + 1)
        return None

    def remove_leaves(self):
        """Merge children into this node; return the number of removed leaves."""
        result = 0
        for node in self.children:
            if node:
                self.color.red += node.color.red
                self.color.green += node.color.green
                self.color.blue += node.color.blue
                self.color.alpha += node.color.alpha
                self.pixel_count += node.pixel_count
                result += 1
        return result - 1

    def get_color_index_for_level(self, color, level):
        """Get the index of *color* for the next *level*."""
        index = 0
        mask = 0x80 >> level
        if color.red & mask:
            index |= 8
        if color.green & mask:
            index |= 4
        if color.blue & mask:
            index |= 2
        if color.alpha & mask:
            index |= 1
        return index

    def get_color(self):
        """Get the average color."""
        count = max(1, self.pixel_count)
        return Color(
            self.color.red / count,
            self.color.green / count,
            self.color.blue / count,
            self.color.alpha / count)


class OctreeQuantizer:
    """Octree quantizer; ``MAX_DEPTH`` limits the number of levels."""

    MAX_DEPTH = 8

    def __init__(self):
        self.levels = {i: [] for i in range(OctreeQuantizer.MAX_DEPTH)}
        self.root = OctreeNode(0, self)

    def get_leaves(self):
        """Get all leaves."""
        return list(self.root.get_leaf_nodes())

    def add_level_node(self, level, node):
        """Add *node* to the nodes at *level*."""
        self.levels[level].append(node)

    def add_color(self, color, count=1):
        """Add *color* to the octree."""
        # passes self as `parent` to save nodes to the levels dict
        self.root.add_color(color, 0, self, count)

    def make_palette(self, color_count):
        """Make a palette with at most *color_count* colors."""
        palette = []
        palette_index = 0
        leaf_count = len(self.get_leaves())
        # reduce nodes
        # up to 16 leaves can be reduced here and the palette will have
        # only 240 colors (in worst case) instead of expected 256 colors
        for level in range(OctreeQuantizer.MAX_DEPTH - 1, -1, -1):
            if self.levels[level]:
                for node in self.levels[level]:
                    leaf_count -= node.remove_leaves()
                    if leaf_count <= color_count:
                        break
                if leaf_count <= color_count:
                    break
                self.levels[level] = []
        # build palette
        for node in self.get_leaves():
            if palette_index >= color_count:
                break
            if node.is_leaf():
                palette.append(node.get_color())
            node.palette_index = palette_index
            palette_index += 1
        return palette

    def get_palette_index(self, color):
        """Get the palette index for *color*."""
        return self.root.get_palette_index(color, 0)
