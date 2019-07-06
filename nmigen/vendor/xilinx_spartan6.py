from abc import abstractproperty

from ..hdl.ast import *
from ..hdl.dsl import *
from ..hdl.ir import *
from ..build import *


__all__ = ["XilinxSpartan6Platform"]


class XilinxSpartan6Platform(TemplatedPlatform):
    """
    Required tools:
        * ISE toolchain:
            * ``xst``
            * ``ngdbuild``
            * ``map``
            * ``par``
            * ``bitgen``

    Available overrides:
        * ``script_after_run``: inserts commands after ``run`` in XST script.
        * ``add_constraints``: inserts commands in UCF file.
        * ``xst_opts``: adds extra options for XST.
        * ``ngdbuild_opts``: adds extra options for NGDBuild.
        * ``map_opts``: adds extra options for MAP.
        * ``par_opts``: adds extra options for PAR.
        * ``bitgen_opts``: adds extra options for BitGen.

    Build products:
        * ``{{name}}.srp``: synthesis report.
        * ``{{name}}.ngc``: synthesized RTL.
        * ``{{name}}.bld``: NGDBuild log.
        * ``{{name}}.ngd``: design database.
        * ``{{name}}_map.map``: MAP log.
        * ``{{name}}_map.mrp``: mapping report.
        * ``{{name}}_map.ncd``: mapped netlist.
        * ``{{name}}.pcf``: physical constraints.
        * ``{{name}}_par.par``: PAR log.
        * ``{{name}}_par_pad.txt``: I/O usage report.
        * ``{{name}}_par.ncd``: place and routed netlist.
        * ``{{name}}.drc``: DRC report.
        * ``{{name}}.bgn``: BitGen log.
        * ``{{name}}.bit``: binary bitstream.
    """

    device  = abstractproperty()
    package = abstractproperty()
    speed   = abstractproperty()

    file_templates = {
        **TemplatedPlatform.build_script_templates,
        "{{name}}.v": r"""
            /* {{autogenerated}} */
            {{emit_design("verilog")}}
        """,
        "{{name}}.prj": r"""
            # {{autogenerated}}
            {% for file in platform.iter_extra_files(".vhd", ".vhdl") -%}
                vhdl work {{file}}
            {% endfor %}
            {% for file in platform.iter_extra_files(".v") -%}
                verilog work {{file}}
            {% endfor %}
            verilog work {{name}}.v
        """,
        "{{name}}.xst": r"""
            # {{autogenerated}}
            run
            -ifn {{name}}.prj
            -ofn {{name}}.ngc
            -top {{name}}
            -p {{platform.device}}{{platform.package}}-{{platform.speed}}
            {{get_override("script_after_run")|default("# (script_after_run placeholder)")}}
        """,
        "{{name}}.ucf": r"""
            # {{autogenerated}}
            {% for port_name, pin_name, attrs in platform.iter_port_constraints_bits() -%}
                {% set port_name = port_name|replace("[", "<")|replace("]", ">") -%}
                NET "{{port_name}}" LOC={{pin_name}};
                {% for attr_name, attr_value in attrs.items() -%}
                    NET "{{port_name}}" {{attr_name}}={{attr_value}};
                {% endfor %}
            {% endfor %}
            {% for signal, frequency in platform.iter_clock_constraints() -%}
                NET "{{signal.name}}" TNM_NET="PRD{{signal.name}}";
                TIMESPEC "TS{{signal.name}}"=PERIOD "PRD{{signal.name}}" {{1000000000/frequency}} ns HIGH 50%;
            {% endfor %}
            {{get_override("add_constraints")|default("# (add_constraints placeholder)")}}
        """
    }
    command_templates = [
        r"""
        {{get_tool("xst")}}
            {{get_override("xst_opts")|options}}
            -ifn {{name}}.xst
        """,
        r"""
        {{get_tool("ngdbuild")}}
            {{quiet("-quiet")}}
            {{verbose("-verbose")}}
            {{get_override("ngdbuild_opts")|options}}
            -uc {{name}}.ucf
            {{name}}.ngc
        """,
        r"""
        {{get_tool("map")}}
            {{verbose("-detail")}}
            {{get_override("map_opts")|default(["-w"])|options}}
            -o {{name}}_map.ncd
            {{name}}.ngd
            {{name}}.pcf
        """,
        r"""
        {{get_tool("par")}}
            {{get_override("par_opts")|default(["-w"])|options}}
            {{name}}_map.ncd
            {{name}}_par.ncd
            {{name}}.pcf
        """,
        r"""
        {{get_tool("bitgen")}}
            {{get_override("bitgen_opts")|default(["-w"])|options}}
            {{name}}_par.ncd
            {{name}}.bit
        """
    ]

    def _get_xdr_buffer(self, m, pin, i_invert=None, o_invert=None):
        def get_dff(clk, d, q):
            # SDR I/O is performed by packing a flip-flop into the pad IOB.
            for bit in range(len(q)):
                _q = Signal()
                _q.attrs["IOB"] = "TRUE"
                m.submodules += Instance("FDCE",
                    i_C=clk,
                    i_CE=Const(1),
                    i_CLR=Const(0),
                    i_D=d[bit],
                    o_Q=_q,
                )
                m.d.comb += q[bit].eq(_q)

        def get_iddr(clk, d, q0, q1):
            for bit in range(len(q0)):
                m.submodules += Instance("IDDR2",
                    p_DDR_ALIGNMENT="C0",
                    p_SRTYPE="ASYNC",
                    p_INIT_Q0=0, p_INIT_Q1=0,
                    i_C0=clk, i_C1=~clk,
                    i_CE=Const(1),
                    i_S=Const(0), i_R=Const(0),
                    i_D=d[bit],
                    o_Q0=q0[bit], o_Q1=q1[bit]
                )

        def get_oddr(clk, d0, d1, q):
            for bit in range(len(q)):
                m.submodules += Instance("ODDR2",
                    p_DDR_ALIGNMENT="C0",
                    p_SRTYPE="ASYNC",
                    p_INIT=0,
                    i_C0=clk, i_C1=~clk,
                    i_CE=Const(1),
                    i_S=Const(0), i_R=Const(0),
                    i_D0=d0[bit], i_D1=d1[bit],
                    o_Q=q[bit]
                )

        def get_ixor(y, invert):
            if invert is None:
                return y
            else:
                a = Signal.like(y, name_suffix="_x{}".format(1 if invert else 0))
                for bit in range(len(y)):
                    m.submodules += Instance("LUT1",
                        p_INIT=0b01 if invert else 0b10,
                        i_I0=a[bit],
                        o_O=y[bit]
                    )
                return a

        def get_oxor(a, invert):
            if invert is None:
                return a
            else:
                y = Signal.like(a, name_suffix="_x{}".format(1 if invert else 0))
                for bit in range(len(a)):
                    m.submodules += Instance("LUT1",
                        p_INIT=0b01 if invert else 0b10,
                        i_I0=a[bit],
                        o_O=y[bit]
                    )
                return y

        if "i" in pin.dir:
            if pin.xdr < 2:
                pin_i  = get_ixor(pin.i, i_invert)
            elif pin.xdr == 2:
                pin_i0 = get_ixor(pin.i0, i_invert)
                pin_i1 = get_ixor(pin.i1, i_invert)
        if "o" in pin.dir:
            if pin.xdr < 2:
                pin_o  = get_oxor(pin.o, o_invert)
            elif pin.xdr == 2:
                pin_o0 = get_oxor(pin.o0, o_invert)
                pin_o1 = get_oxor(pin.o1, o_invert)

        i = o = t = None
        if "i" in pin.dir:
            i = Signal(pin.width, name="{}_xdr_i".format(pin.name))
        if "o" in pin.dir:
            o = Signal(pin.width, name="{}_xdr_o".format(pin.name))
        if pin.dir in ("oe", "io"):
            t = Signal(1,         name="{}_xdr_t".format(pin.name))

        if pin.xdr == 0:
            if "i" in pin.dir:
                i = pin_i
            if "o" in pin.dir:
                o = pin_o
            if pin.dir in ("oe", "io"):
                t = ~pin.oe
        elif pin.xdr == 1:
            if "i" in pin.dir:
                get_dff(pin.i_clk, i, pin_i)
            if "o" in pin.dir:
                get_dff(pin.o_clk, pin_o, o)
            if pin.dir in ("oe", "io"):
                get_dff(pin.o_clk, ~pin.oe, t)
        elif pin.xdr == 2:
            if "i" in pin.dir:
                # Re-register first input before it enters fabric. This allows both inputs to
                # enter fabric on the same clock edge, and adds one cycle of latency.
                i0_ff = Signal.like(pin_i0, name_suffix="_ff")
                get_dff(pin.i_clk, i0_ff, pin_i0)
                get_iddr(pin.i_clk, i, i0_ff, pin_i1)
            if "o" in pin.dir:
                get_oddr(pin.o_clk, pin_o0, pin_o1, o)
            if pin.dir in ("oe", "io"):
                get_dff(pin.o_clk, ~pin.oe, t)
        else:
            assert False

        return (i, o, t)

    def get_input(self, pin, port, attrs, invert):
        self._check_feature("single-ended input", pin, attrs,
                            valid_xdrs=(0, 1, 2), valid_attrs=True)
        m = Module()
        i, o, t = self._get_xdr_buffer(m, pin, i_invert=True if invert else None)
        for bit in range(len(port)):
            m.submodules[pin.name] = Instance("IBUF",
                i_I=port[bit],
                o_O=i[bit]
            )
        return m

    def get_output(self, pin, port, attrs, invert):
        self._check_feature("single-ended output", pin, attrs,
                            valid_xdrs=(0, 1, 2), valid_attrs=True)
        m = Module()
        i, o, t = self._get_xdr_buffer(m, pin, o_invert=True if invert else None)
        for bit in range(len(port)):
            m.submodules[pin.name] = Instance("OBUF",
                i_I=o[bit],
                o_O=port[bit]
            )
        return m

    def get_tristate(self, pin, port, attrs, invert):
        self._check_feature("single-ended tristate", pin, attrs,
                            valid_xdrs=(0, 1, 2), valid_attrs=True)
        m = Module()
        i, o, t = self._get_xdr_buffer(m, pin, o_invert=True if invert else None)
        for bit in range(len(port)):
            m.submodules[pin.name] = Instance("OBUFT",
                i_T=t,
                i_I=o[bit],
                o_O=port[bit]
            )
        return m

    def get_input_output(self, pin, port, attrs, invert):
        self._check_feature("single-ended input/output", pin, attrs,
                            valid_xdrs=(0, 1, 2), valid_attrs=True)
        m = Module()
        i, o, t = self._get_xdr_buffer(m, pin, i_invert=True if invert else None,
                                               o_invert=True if invert else None)
        for bit in range(len(port)):
            m.submodules[pin.name] = Instance("IOBUF",
                i_T=t,
                i_I=o[bit],
                o_O=i[bit],
                io_IO=port[bit]
            )
        return m

    def get_diff_input(self, pin, p_port, n_port, attrs, invert):
        self._check_feature("differential input", pin, attrs,
                            valid_xdrs=(0, 1, 2), valid_attrs=True)
        m = Module()
        i, o, t = self._get_xdr_buffer(m, pin, i_invert=True if invert else None)
        for bit in range(len(p_port)):
            m.submodules[pin.name] = Instance("IBUFDS",
                i_I=p_port[bit], i_IB=n_port[bit],
                o_O=i[bit]
            )
        return m

    def get_diff_output(self, pin, p_port, n_port, attrs, invert):
        self._check_feature("differential output", pin, attrs,
                            valid_xdrs=(0, 1, 2), valid_attrs=True)
        m = Module()
        i, o, t = self._get_xdr_buffer(m, pin, o_invert=True if invert else None)
        for bit in range(len(p_port)):
            m.submodules[pin.name] = Instance("OBUFDS",
                i_I=o[bit],
                o_O=p_port[bit], o_OB=n_port[bit]
            )
        return m

    def get_diff_tristate(self, pin, p_port, n_port, attrs, invert):
        self._check_feature("differential tristate", pin, attrs,
                            valid_xdrs=(0, 1, 2), valid_attrs=True)
        m = Module()
        i, o, t = self._get_xdr_buffer(m, pin, o_invert=True if invert else None)
        for bit in range(len(p_port)):
            m.submodules[pin.name] = Instance("OBUFTDS",
                i_T=t,
                i_I=o[bit],
                o_O=p_port[bit], o_OB=n_port[bit]
            )
        return m

    def get_diff_input_output(self, pin, p_port, n_port, attrs, invert):
        self._check_feature("differential input/output", pin, attrs,
                            valid_xdrs=(0, 1, 2), valid_attrs=True)
        m = Module()
        i, o, t = self._get_xdr_buffer(m, pin, i_invert=True if invert else None,
                                               o_invert=True if invert else None)
        for bit in range(len(p_port)):
            m.submodules[pin.name] = Instance("IOBUFDS",
                i_T=t,
                i_I=o[bit],
                o_O=i[bit],
                io_IO=p_port[bit], io_IOB=n_port[bit]
            )
        return m
