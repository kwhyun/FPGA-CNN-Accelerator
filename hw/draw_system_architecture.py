#!/usr/bin/env python3
"""Draw the paper architecture from the implemented RTL and PS7 integration.

Run with Python for SVG; add --render with CairoSVG installed for PNG.
Only the Python standard library is needed to generate and check the SVG.
Functional groups deliberately hide port muxes, valid/ready and control fanout.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from dataclasses import dataclass
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT = 3160, 1680
INK = '#111111'


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float
    group: bool = False


class Figure:
    def __init__(self):
        self.parts: list[str] = []
        self.boxes: dict[str, Box] = {}
        self.routes: dict[str, list[tuple[float, float]]] = {}
        self.labels: list[tuple[float, float, str, int, str]] = []

    def text(self, x, y, value, size=30, *, bold=False, anchor='middle'):
        for i, line in enumerate(value.split('\n')):
            yy = y + i * size * 1.3
            self.labels.append((x, yy, line, size, anchor))
            self.parts.append(
                f'<text x="{x}" y="{yy}" font-size="{size}" '
                f'text-anchor="{anchor}" font-weight="{600 if bold else 400}">'
                f'{html.escape(line)}</text>'
            )

    def rect(self, key, x, y, w, h, *, group=False, weight=2.2, dash=False):
        assert key not in self.boxes
        self.boxes[key] = Box(x, y, w, h, group)
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="white" '
            f'stroke="{INK}" stroke-width="{weight}"'
            + (' stroke-dasharray="10 7"' if dash else '') + '/>'
        )

    def block(self, key, x, y, w, h, title, subtitle='', *, size=32, subtitle_bold=False):
        self.rect(key, x, y, w, h)
        lines = 1 + (len(subtitle.split('\n')) if subtitle else 0)
        base = y + h / 2 - (lines - 1) * 18 + 11
        self.text(x + w / 2, base, title, size, bold=True)
        if subtitle:
            self.text(x + w / 2, base + 43, subtitle, 27, bold=subtitle_bold)

    def group(self, key, x, y, w, h, title, *, size=31, dash=False):
        self.rect(key, x, y, w, h, group=True, weight=2.5, dash=dash)
        self.text(x + 22, y + 39, title, size, bold=True, anchor='start')

    def wire(self, key, points, *, control=False, both=False, width=3):
        assert key not in self.routes
        self.routes[key] = points
        d = ' '.join(f'{x},{y}' for x, y in points)
        self.parts.append(
            f'<polyline id="{key}" points="{d}" fill="none" stroke="{INK}" '
            f'stroke-width="{width}" stroke-linejoin="miter" marker-end="url(#arrow)"'
            + (' marker-start="url(#arrow-start)"' if both else '')
            + (' stroke-dasharray="10 7"' if control else '') + '/>'
        )

    def divider(self, x1, y1, x2, y2, width=1.7):
        self.parts.append(f'<path d="M{x1} {y1}H{x2}" stroke="{INK}" stroke-width="{width}" fill="none"/>'
                          if y1 == y2 else
                          f'<path d="M{x1} {y1}L{x2} {y2}" stroke="{INK}" stroke-width="{width}" fill="none"/>')

    def dot(self, x, y):
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="4.5" fill="{INK}"/>')

    def memory(self, key, x, y, w, h, title, subtitle, *, size=32):
        # Two sheets denote two banks, not extra capacities.
        self.rect(key + '_back', x + 9, y - 9, w, h, group=True, weight=1.5)
        self.block(key, x, y, w, h, title, subtitle, size=size)

    def array(self, key, x, y, w, h, index):
        self.rect(key, x, y, w, h)
        self.text(x + w/2, y + 37, f'PE array {index}', 31, bold=True)
        self.text(x + w/2, y + 76, '8 × 8 INT8 MACs', 27)
        cell, gap = 12, 7
        grid_w = cell*8 + gap*7
        x0, y0 = x+(w-grid_w)/2, y+100
        for r in range(8):
            for c in range(8):
                self.parts.append(f'<rect x="{x0+c*(cell+gap)}" y="{y0+r*(cell+gap)}" '
                                  f'width="{cell}" height="{cell}" fill="white" stroke="{INK}" stroke-width="1"/>')

    def check(self):
        """Reject nonorthogonal routes, routes through leaf blocks, and crossings."""
        segments = []
        for key, points in self.routes.items():
            for p, q in zip(points, points[1:]):
                assert p != q, ('zero segment', key)
                assert p[0] == q[0] or p[1] == q[1], ('diagonal', key, p, q)
                assert all(0 <= x <= WIDTH and 0 <= y <= HEIGHT for x, y in (p, q))
                segments.append((key, p, q))
                for name, b in self.boxes.items():
                    if b.group:
                        continue
                    # Touching a port/border is allowed; entering a leaf interior is not.
                    if p[1] == q[1]:
                        hit = b.y+1 < p[1] < b.y+b.h-1 and max(min(p[0],q[0]),b.x+1) < min(max(p[0],q[0]),b.x+b.w-1)
                    else:
                        hit = b.x+1 < p[0] < b.x+b.w-1 and max(min(p[1],q[1]),b.y+1) < min(max(p[1],q[1]),b.y+b.h-1)
                    assert not hit, ('wire intersects block',key,name,p,q)
        for i,(ka,p,q) in enumerate(segments):
            for kb,r,s in segments[i+1:]:
                if ka == kb or (p[0] == q[0]) == (r[0] == s[0]):
                    continue
                vp,vq,hp,hq = (p,q,r,s) if p[0] == q[0] else (r,s,p,q)
                cross = min(hp[0],hq[0]) < vp[0] < max(hp[0],hq[0]) and min(vp[1],vq[1]) < hp[1] < max(vp[1],vq[1])
                assert not cross, ('wire crossing', ka, kb)
        assert all(0 < x < WIDTH and 0 < y < HEIGHT for x,y,*_ in self.labels)

    def svg(self, evidence):
        description = (
            'Monochrome system architecture of the implemented FPGA CNN accelerator. '
            'Solid arrows show payload movement; dashed arrows show commands and control. '
            'The AXI data links include protocol conversion for the processing system. '
            'Operand preparation groups top-level read-port steering with matrix_agu and vector_agu; '
            'the matrix AGU supports CONV, pointwise, depthwise and fully connected layers. '
            'drain includes two quantization units and vector output bypass. '
            'Block scheduler groups plan_unit, four block_iterator, compute_issue and four pingpong_ctrl. '
            'Internal control fanout and valid/ready signals are omitted.'
        )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="240mm" height="{240*HEIGHT/WIDTH:.6f}mm" '
            f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title desc">'
            '<title id="title">FPGA CNN accelerator system architecture</title>'
            f'<desc id="desc">{html.escape(description)}</desc>'
            f'<metadata>{html.escape(json.dumps(evidence,ensure_ascii=False))}</metadata>'
            '<defs>'
            '<marker id="arrow" viewBox="0 0 12 10" refX="11" refY="5" markerWidth="12" markerHeight="10" '
            'markerUnits="userSpaceOnUse" orient="auto"><path d="M0 0L12 5L0 10Z" fill="#111111"/></marker>'
            '<marker id="arrow-start" viewBox="0 0 12 10" refX="1" refY="5" markerWidth="12" markerHeight="10" '
            'markerUnits="userSpaceOnUse" orient="auto"><path d="M12 0L0 5L12 10Z" fill="#111111"/></marker>'
            '</defs>'
            f'<rect width="{WIDTH}" height="{HEIGHT}" fill="white"/>'
            f'<g font-family="DejaVu Sans, sans-serif" fill="{INK}">'
            + ''.join(self.parts) + '</g></svg>\n'
        )


def evidence():
    refs = {
        'rtl/accel_top.sv': ['u_control','u_layer','u_plan','u_input_pingpong','u_weight_pingpong',
                             'u_coefficient_pingpong','u_accumulator_pingpong','u_input_loader',
                             'u_weight_loader','u_compute','u_drain','u_input_dma',
                             'u_weight_dma','u_output_dma','u_matrix_agu','u_vector_agu',
                             'u_vector','u_input0','u_input1','u_weight0_0','u_weight0_1',
                             'u_weight1_0','u_weight1_1','u_coefficient0','u_coefficient1',
                             'u_array0','u_array1','u_accumulator0','u_accumulator1'],
        'rtl/drain.sv': ['coefficient_q','u_quantization','vector_data_i'],
        'rtl/ws_array.sv': ['DIM','u_pe'],
        'rtl/accumulator.sv': ['DEPTH','DATA_W','u_ram'],
        'hw/system_impl.tcl': ['S_AXI_HP0','S_AXI_HP1','S_AXI_HP2','M_AXI_GP0','IRQ_F2P'],
        'docs/architecture.md': ['CONV','PW','FC','DWCONV'],
        'docs/ISA.md': ['CONV','FC'],
    }
    result = {}
    for name, tokens in refs.items():
        data=(ROOT/name).read_bytes()
        source=data.decode()
        for token in tokens:
            assert (token in source if name.startswith('docs/') else
                    re.search(r'\b'+re.escape(token)+r'\b',source)), (name,token)
        result[name]={'sha256':hashlib.sha256(data).hexdigest(),'references':tokens}
    top=(ROOT/'rtl/accel_top.sv').read_text()
    assert len(re.findall(r'block_iterator\s*#',top)) == 4
    assert len(re.findall(r'pingpong_ctrl\s+u_',top)) == 4
    assert len(re.findall(r'ws_array\s*#',top)) == 2
    assert len(re.findall(r'accumulator\s+u_',top)) == 2
    return result


def build():
    f=Figure()
    # Processing-system implementation names stay in evidence(), not in figure labels.
    f.group('ps',40,70,330,1350,'Processing system',size=28)
    f.block('host',65,145,280,170,'ARM Cortex-A9','Host software',size=31)
    f.block('psmem',65,485,280,905,'PS memory','DDR controller\n\nAXI interfaces')
    f.block('ddr',65,1470,280,125,'External DDR3','Instructions / data')
    f.wire('host_memory',[(205,315),(205,485)],both=True)
    f.wire('ddr_bus',[(205,1390),(205,1470)],both=True,width=4)
    f.text(226,1448,'DDR',29,anchor='start')

    f.group('accelerator',450,25,2685,1570,'FPGA CNN accelerator · 100 MHz',size=35)
    f.group('control_group',500,85,2580,285,'Control')
    f.block('csr',530,150,300,145,'Control / status','AXI-Lite · IRQ',size=30)
    f.block('layer',920,150,320,145,'Layer control','Fetch · decode')
    # plan_unit, block_iterator ×4, compute_issue and pingpong_ctrl ×4 are merged into one box.
    f.block('scheduler',1330,150,1710,145,'Block scheduler','Plan · block iteration · ping-pong banks')
    f.wire('host_csr',[(345,195),(530,195)],both=True)
    f.text(400,176,'AXI',25)
    f.wire('irq',[(530,270),(345,270)],control=True)
    f.text(400,254,'IRQ',26)
    f.wire('start',[(830,215),(920,215)],control=True)
    f.wire('layer_config',[(1240,222),(1330,222)],control=True)

    f.rect('dataplane',480,475,2620,1090,group=True,weight=2.5)
    f.text(1780,514,'Datapath',31,bold=True)
    f.wire('control_datapath',[(2185,295),(2185,475)],control=True)  # center of Block scheduler

    f.group('loaders',505,530,285,685,'Load engines',size=29)
    f.block('input_dma',525,590,245,90,'Read DMA',size=30)
    f.block('input_loader',525,735,245,90,'Input loader',size=30)
    f.block('weight_dma',525,910,245,90,'Read DMA',size=30)
    f.block('weight_loader',525,1055,245,125,'Weight / coef.','loader',size=29,subtitle_bold=True)
    f.wire('input_stream',[(647.5,680),(647.5,735)])
    f.wire('weight_stream',[(647.5,1000),(647.5,1055)])
    f.wire('axi_input',[(345,635),(525,635)],width=4)
    f.text(405,613,'AXI4',25)
    f.wire('axi_weight',[(345,955),(525,955)],width=4)
    f.text(405,933,'AXI4',25)
    f.wire('instruction_fetch',[(770,620),(875,620),(875,250),(920,250)])
    f.text(895,419,'Instructions',27,anchor='start')

    f.memory('input_mem',905,710,280,140,'Input SRAM','2 × 64 KiB')
    f.memory('weight_mem',905,900,280,140,'Weight SRAM','2 × 2 × 8 KiB')
    f.memory('coef_mem',905,1090,280,140,'Coefficient SRAM','2 × 512 B',size=27)
    f.wire('load_input',[(770,780),(905,780)])
    f.wire('load_weight',[(770,1090),(835,1090),(835,970),(905,970)])
    f.wire('load_coefficient',[(770,1155),(905,1155)])

    # AGUs share a preparation column. All three execution inputs flow right.
    f.rect('prepare',1280,550,270,830,group=True,weight=2.5)
    f.text(1415,650,'Matrix AGU',30,bold=True)
    f.text(1415,700,'CONV / PW\nDW / FC',26)
    f.divider(1300,1185,1530,1185)
    f.text(1415,1268,'Vector AGU',30,bold=True)
    f.text(1415,1320,'ADD / pooling',27)
    f.wire('input_operands',[(1185,780),(1240,780),(1240,720),(1280,720)])
    f.wire('weight_operands',[(1185,970),(1280,970)])

    # Matrix+ACC and vector occupy the same execution column.
    # Arrays and their accumulators are drawn as two independent horizontal lanes.
    f.group('tensor',1670,530,560,650,'Matrix unit + ACC',size=31)
    f.array('array0',1690,590,245,260,0)
    f.array('array1',1690,890,245,260,1)
    f.block('acc0',2020,650,190,140,'ACC 0','2 × 16 KiB',size=30)
    f.block('acc1',2020,950,190,140,'ACC 1','2 × 16 KiB',size=30)
    f.wire('array0_operands',[(1550,720),(1690,720)],width=4)
    f.wire('array1_operands',[(1550,1020),(1690,1020)],width=4)
    f.text(1610,697,'INT8',26)
    f.text(1610,997,'INT8',26)
    f.wire('psum0',[(1935,720),(2020,720)],width=4)
    f.wire('psum1',[(1935,1020),(2020,1020)],width=4)
    f.block('vector',1670,1240,560,140,'Vector unit','8 lanes · ADD / MAX / AVG',size=32)
    f.wire('vector_operands',[(1550,1310),(1670,1310)],width=4)
    f.text(1610,1287,'INT8',26)

    # Quantization stays inside drain; the vector path explicitly bypasses it.
    f.group('drain',2350,590,730,915,'Drain / quantization',size=29)
    f.block('requant0',2380,650,370,140,'Quantization 0','8 lanes · INT32 → INT8',size=29)
    f.block('requant1',2380,950,370,140,'Quantization 1','8 lanes · INT32 → INT8',size=29)
    f.wire('acc0_requant',[(2210,720),(2380,720)],width=4)
    f.wire('acc1_requant',[(2210,1020),(2380,1020)],width=4)
    f.text(2295,697,'INT32',26)
    f.text(2295,997,'INT32',26)
    f.rect('assembly',2840,650,210,730)
    f.text(2945,970,'Output\nassembly',31,bold=True)
    f.wire('requant0_output',[(2750,720),(2840,720)],width=4)
    f.wire('requant1_output',[(2750,1020),(2840,1020)],width=4)
    f.text(2795,697,'INT8',26)
    f.text(2795,997,'INT8',26)
    f.wire('vector_bypass',[(2230,1310),(2840,1310)],width=4)
    # Cache distribution to the two requants is summarized within the drain boundary.
    f.wire('drain_coef',[(1185,1160),(1230,1160),(1230,1470),(2350,1470)])
    f.text(1780,1452,'Coefficients',27)

    f.block('output_dma',525,1290,245,140,'Write DMA','AXI4 · 64 b',size=30)
    f.wire('output_stream',[(2945,1380),(2945,1530),(835,1530),(835,1360),(770,1360)],width=4)
    f.text(1780,1515,'INT8 output',28)
    f.wire('axi_output',[(525,1360),(345,1360)],width=4)
    f.text(405,1338,'AXI4',25)

    f.wire('legend_data',[(65,1640),(150,1640)],width=3)
    f.text(170,1651,'Payload',27,anchor='start')
    f.wire('legend_control',[(385,1640),(470,1640)],control=True)
    f.text(490,1651,'Command / control',27,anchor='start')
    f.check()
    return f


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--render',action='store_true',help='also render PNG using CairoSVG')
    parser.add_argument('--output-dir',type=Path,default=ROOT/'pictures')
    args=parser.parse_args()
    ev=evidence()
    figure=build()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    base=args.output_dir/'system_architecture'
    svg=figure.svg(ev)
    base.with_suffix('.svg').write_text(svg)
    if args.render:
        import cairosvg
        from PIL import Image
        cairosvg.svg2png(bytestring=svg.encode(),write_to=str(base.with_suffix('.png')),output_width=6200)
        # Fontconfig can enable colored LCD subpixel antialiasing even for black SVG text.
        # Export a single grayscale channel so the raster artifact is strictly monochrome.
        with Image.open(base.with_suffix('.png')) as rendered:
            monochrome=rendered.convert('L')
        monochrome.save(base.with_suffix('.png'),dpi=(6200/240*25.4,6200/240*25.4),optimize=True)
    print(f'Validated {len(figure.boxes)} block rectangles and {len(figure.routes)} orthogonal routes; no leaf-block intersections or wire crossings.')
    print(f'Wrote {base}' + ('.{svg,png}' if args.render else '.svg'))


if __name__=='__main__':
    main()
