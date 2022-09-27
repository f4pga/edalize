# Copyright edalize contributors
# Licensed under the 2-Clause BSD License, see LICENSE for details.
# SPDX-License-Identifier: BSD-2-Clause

import logging
import os.path
import platform
import re
import subprocess

from edalize.edatool import Edatool
from edalize.utils import EdaCommands
from edalize.yosys import Yosys
from edalize.surelog import Surelog
from importlib import import_module

logger = logging.getLogger(__name__)

""" Symbiflow backend

A core (usually the system core) can add the following files:

- Standard design sources (Verilog only)

- Constraints: unmanaged constraints with file_type SDC, pin_constraints with file_type PCF and placement constraints with file_type xdc

"""


class Symbiflow(Edatool):

    argtypes = ["vlogdefine", "vlogparam", "generic"]
    archs = ["xilinx", "fpga_interchange"]
    fpga_interchange_families = ["xc7"]

    @classmethod
    def get_doc(cls, api_ver):
        if api_ver == 0:
            symbiflow_help = {
                "members": [
                    {
                        "name": "arch",
                        "type": "String",
                        "desc": "Target architecture. Legal values are *xilinx* and *fpga_interchange* (this is relevant only for Nextpnr variant).",
                    },
                    {
                        "name": "package",
                        "type": "String",
                        "desc": "FPGA chip package (e.g. clg400-1)",
                    },
                    {
                        "name": "part",
                        "type": "String",
                        "desc": "FPGA part type (e.g. xc7a50t)",
                    },
                    {
                        "name": "vendor",
                        "type": "String",
                        "desc": 'Target architecture. Currently only "xilinx" is supported',
                    },
                    {
                        "name": "pnr",
                        "type": "String",
                        "desc": 'Place and Route tool. Currently only "vpr"/"vtr" and "nextpnr" are supported',
                    },
                    {
                        "name": "vpr_options",
                        "type": "String",
                        "desc": "Additional options for VPR tool. If not used, default options for the tool will be used",
                    },
                    {
                        "name": "nextpnr_options",
                        "type": "String",
                        "desc": "Additional options for Nextpnr tool. If not used, default options for the tool will be used",
                    },
                    {
                        "name": "fasm2bels",
                        "type": "Boolean",
                        "desc": "Value to state whether fasm2bels is to be used"
                    },
                    {
                        "name": "dbroot",
                        "type": "String",
                        "desc": "Path to the database root (needed by fasm2bels)."
                    },
                    {
                        "name": "clocks",
                        "type": "dict",
                        "desc": "Clocks to be added for having tools correctly handling timing based routing."
                    },
                    {
                        "name": "seed",
                        "type": "String",
                        "desc": "Seed assigned to the PnR tool."
                    },
                    {
                        "name": "schema_dir",
                        "type": "String",
                        "desc": "Path if Capnp schema used by fpga_interchange",
                    },
                ],
            }

            symbiflow_members = symbiflow_help["members"]

            return {
                "description": "The Symbiflow backend executes Yosys sythesis tool and VPR/Nextpnr place and route. It can target multiple different FPGA vendors",
                "members": symbiflow_members,
            }

    def get_version(self):
        return "1.0"

    def configure_nextpnr(self):
        (src_files, incdirs) = self._get_fileset_files(force_slash=True)
        vendor = self.tool_options.get("vendor")

        # Yosys configuration
        yosys_synth_options = self.tool_options.get("yosys_synth_options", "")
        yosys_additional_commands = self.tool_options.get("yosys_additional_commands", "")
        yosys_template = self.tool_options.get("yosys_template")
        yosys_edam = {
            "files": self.files,
            "name": self.name,
            "toplevel": self.toplevel,
            "parameters": self.parameters,
            "tool_options": {
                "yosys": {
                    "arch": vendor,
                    "output_format": "json",
                    "yosys_synth_options": yosys_synth_options,
                    "yosys_additional_commands" : yosys_additional_commands,
                    "yosys_read_options" : self.tool_options.get("yosys_read_options", []),
                    "yosys_template": yosys_template,
                    "yosys_as_subtool": True,
                    "frontend_options" : self.tool_options.get("frontend_options", []),
                }
            },
        }

        yosys = getattr(import_module("edalize.yosys"), "Yosys")(
            yosys_edam, self.work_root
        )
        yosys.configure()

        # Nextpnr configuration
        arch = self.tool_options.get("arch")
        if arch not in self.archs:
            logger.error(
                'Missing or invalid "arch" parameter: {} in "tool_options"'.format(arch)
            )

        package = self.tool_options.get("package")
        if not package:
            logger.error('Missing required "package" parameter')

        part = self.tool_options.get("part")
        if not part:
            logger.error('Missing required "part" parameter')

        target_family = None
        for family in getattr(self, "fpga_interchange_families"):
            if family in part:
                target_family = family
                break

        if target_family is None and arch == "fpga_interchange":
            logger.error(
                "Couldn't find family for part: {}. Available families: {}".format(
                    part, ", ".join(getattr(self, "fpga_interchange_families"))
                )
            )

        chipdb = None
        device = None
        placement_constraints = []
        vpr_grid = None
        rr_graph = None
        vpr_capnp_schema = None


        for f in src_files:
            if f.file_type in ["bba"]:
                chipdb = f.name
            elif f.file_type in ["device"]:
                device = f.name
            elif f.file_type in ["xdc"]:
                placement_constraints.append(f.name)
            elif f.file_type in ["RRGraph"]:
                rr_graph = f.name
            elif f.file_type in ["VPRGrid"]:
                vpr_grid = f.name
            elif f.file_type in ['capnp']:
                vpr_capnp_schema = f.name
            else:
                continue

        if not chipdb:
            logger.error("Missing required chipdb file")

        if placement_constraints == []:
            logger.error("Missing required XDC file(s)")

        if device is None and arch == "fpga_interchange":
            logger.error('Missing required ".device" file for "fpga_interchange" arch')

        nextpnr_options = self.tool_options.get("nextpnr_options", "")
        partname = part + package
        # Strip speedgrade string when using fpga_interchange
        package = package.split("-")[0] if arch == "fpga_interchange" else None

        if "xc7a" in part:
            bitstream_device = "artix7"
        if "xc7z" in part:
            bitstream_device = "zynq7"
        if "xc7k" in part:
            bitstream_device = "kintex7"

        depends = self.name + ".json"
        xdcs = []
        for x in placement_constraints:
            xdcs += ["--xdc", x]

        clocks = self.tool_options.get('clocks', dict())

        commands = EdaCommands()
        commands.commands = yosys.commands
        if arch == "fpga_interchange":
            self.render_template('interchange-tcl.j2',
                                 'interchange.tcl',
                                 dict(name=self.name, clocks=clocks, part=part))
            self.render_template('vivado-sh.j2',
                                 'vivado.sh',
                                 dict(tcl="interchange"))

            assert len(xdcs) == 2, xdcs

            schema_dir = self.tool_options.get("schema_dir", None)
            if schema_dir is None:
                commands.header += """ifndef INTERCHANGE_SCHEMA_PATH
$(error Environment variable INTERCHANGE_SCHEMA_PATH was not found. It should be set to <fpga-interchange-schema path>/interchange)
endif
"""
            commands.header += """ifndef RAPIDWRIGHT_PATH
$(error Environment variable RAPIDWRIGHT_PATH was not found. It should be set to <rapid wright path>)
endif
"""
            targets = self.name + ".netlist"
            command = ["python", "-m", "fpga_interchange.yosys_json"]
            command += ["--schema_dir", "$(INTERCHANGE_SCHEMA_PATH)" if schema_dir is None else schema_dir]
            command += ["--device", device]
            command += ["--top", self.toplevel]
            command += [depends, targets]
            commands.add(command, [targets], [depends])

            depends = self.name + ".netlist"
            targets = self.name + ".phys"
            command = ["nextpnr-" + arch, "--chipdb", chipdb]
            command += ["--package", package]
            command += xdcs
            command += ["--netlist", depends]
            command += ["--write", self.name + ".routed.json"]
            command += ["--phys", targets]
            command += [nextpnr_options]
            commands.add(command, [targets], [depends])

            depends = self.name + ".phys"
            targets = self.name + ".fasm"
            command = ["python", "-m", "fpga_interchange.fasm_generator"]
            command += ["--schema_dir", "$(INTERCHANGE_SCHEMA_PATH)" if schema_dir is None else schema_dir]
            command += [
                "--family",
                family,
                device,
                self.name + ".netlist",
                depends,
                targets,
            ]
            commands.add(command, [targets], [depends])

            depends = self.name+'.fasm'
            targets = self.name+'.dcp'
            command = ['RAPIDWRIGHT_PATH=${RAPIDWRIGHT_PATH} ${RAPIDWRIGHT_PATH}/scripts/invoke_rapidwright.sh com.xilinx.rapidwright.interchange.PhysicalNetlistToDcp']
            command += [self.name+'.netlist', self.name+'.phys', xdcs[1], self.name+'.dcp',]
            commands.add(command, [targets], [depends])

            depends = self.name+'.dcp'
            targets = self.name+'.timing'
            command = ["bash vivado.sh"]
            commands.add(command, [targets], [depends])
        else:
            targets = self.name + ".fasm"
            command = ["nextpnr-" + arch, "--chipdb", chipdb]
            command += xdcs
            command += ["--json", depends]
            command += ["--write", self.name + ".routed.json"]
            command += ["--fasm", targets]
            command += ["--log", "nextpnr.log"]
            command += [nextpnr_options]
            commands.add(command, [targets], [depends])

        depends = self.name + ".fasm"
        targets = self.name + ".bit"
        command = ["symbiflow_write_bitstream", "-d", bitstream_device]
        command += ["-f", depends, "-p", partname, "-b", targets]
        commands.add(command, [targets], [depends])

        fasm2bels = self.tool_options.get('fasm2bels', False)
        dbroot = self.tool_options.get('dbroot', None)
        clocks = self.tool_options.get('clocks', None)
        if fasm2bels:
            if any(v is None for v in [rr_graph, vpr_grid, dbroot]):
                logger.error("When using fasm2bels, rr_graph, vpr_grid and database root must be provided")
            tcl_params = {
                'top': self.toplevel,
                'part': partname,
                'xdc': ' '.join(placement_constraints),
                'clocks': clocks,
            }

            self.render_template('symbiflow-fasm2bels-tcl.j2',
                                 'fasm2bels.tcl',
                                 tcl_params)
            self.render_template('vivado-sh.j2',
                                 'vivado.sh',
                                 dict(tcl="fasm2bels"))

            targets = self.toplevel+".bit.v"
            command = ['python -m fasm2bels']
            command += ['--db_root', dbroot+f'/{bitstream_device}']
            command += ['--part', partname]
            command += ['--bitread bitread']
            command += ['--bit_file', self.toplevel+'.bit']
            command += ['--fasm_file', self.toplevel+'.bit.fasm']
            command += ['--connection_database channels.db']
            command += ['--rr_graph', rr_graph]
            command += ['--route_file', self.toplevel+".route"]
            command += ['--vpr_grid_map', vpr_grid]
            command += ['--vpr_capnp_schema_dir', vpr_capnp_schema]
            command += ['--verilog_file', self.toplevel+".bit.v"]
            command += ['--xdc_file', self.toplevel+".bit.xdc"]
            command += ["&& rm channels.db"]
            commands.add(command, [targets], [])
            depends = targets
            targets = "timing_summary.rpt"
            command = ["bash vivado.sh"]
            commands.add(command, [targets], [depends])

        commands.set_default_target(targets)
        commands.write(os.path.join(self.work_root, "Makefile"))

    def configure_vpr(self):
        (src_files, incdirs) = self._get_fileset_files(force_slash=True)

        has_vhdl     = "vhdlSource"      in [x.file_type for x in src_files]
        has_vhdl2008 = "vhdlSource-2008" in [x.file_type for x in src_files]

        if has_vhdl or has_vhdl2008:
            logger.error("VHDL files are not supported in Yosys")
        file_list = []
        timing_constraints = []
        pins_constraints = []
        placement_constraints = []

        vpr_grid = None
        rr_graph = None
        vpr_capnp_schema = None

        for f in src_files:
            if f.file_type in ["verilogSource"]:
                file_list.append(f.name)
            if f.file_type in ["SDC"]:
                timing_constraints.append(f.name)
            if f.file_type in ["PCF"]:
                pins_constraints.append(f.name)
            if f.file_type in ["xdc"]:
                placement_constraints.append(f.name)
            if f.file_type in ["RRGraph"]:
                rr_graph = f.name
            if f.file_type in ["VPRGrid"]:
                vpr_grid = f.name
            if f.file_type in ["capnp"]:
                vpr_capnp_schema = f.name

        builddir = self.tool_options.get('builddir', 'build')

        part = self.tool_options.get("part")
        package = self.tool_options.get("package")
        vendor = self.tool_options.get("vendor")

        if not part:
            logger.error('Missing required "part" parameter')
        if not package:
            logger.error('Missing required "package" parameter')

        if vendor == "xilinx":
            if "xc7a" in part:
                bitstream_device = "artix7"
            if "xc7z" in part:
                bitstream_device = "zynq7"
            if "xc7k" in part:
                bitstream_device = "kintex7"

            partname = part + package

            # a35t are in fact a50t
            # leave partname with 35 so we access correct DB
            if part == "xc7a35t":
                part = "xc7a50t"
            device_suffix = "test"
        elif vendor == "quicklogic":
            partname = package
            device_suffix = "wlcsp"
            bitstream_device = part + "_" + device_suffix

        _vo = self.tool_options.get("vpr_options")
        vpr_options = ["--additional_vpr_options", f'"{_vo}"'] if _vo else []
        pcf_opts = ["-p"] + pins_constraints if pins_constraints else []
        sdc_opts = ["-s"] + timing_constraints if timing_constraints else []
        xdc_opts = ["-x"] + placement_constraints if placement_constraints else []

        fasm2bels = self.tool_options.get('fasm2bels', False)
        dbroot = self.tool_options.get('dbroot', None)
        clocks = self.tool_options.get('clocks', None)

        if fasm2bels:
            if any(v is None for v in [rr_graph, vpr_grid, dbroot]):
                logger.error("When using fasm2bels, rr_graph, vpr_grid and database root must be provided")
            tcl_params = {
                'top': self.toplevel,
                'part': partname,
                'xdc': ' '.join(placement_constraints),
                'clocks': clocks,
            }

            self.render_template('symbiflow-fasm2bels-tcl.j2',
                                 'fasm2bels.tcl',
                                 tcl_params)
            self.render_template('vivado-sh.j2',
                                 'vivado.sh',
                                 dict())

        seed = self.tool_options.get('seed', None)

        fasm2bels = self.tool_options.get('fasm2bels', False)
        dbroot = self.tool_options.get('dbroot', None)
        clocks = self.tool_options.get('clocks', None)

        if fasm2bels:
            if any(v is None for v in [rr_graph, vpr_grid, dbroot]):
                logger.error("When using fasm2bels, rr_graph, vpr_grid and database root must be provided")
            tcl_params = {
                'top': self.toplevel,
                'part': partname,
                'xdc': ' '.join(placement_constraints),
                'clocks': clocks,
            }

            self.render_template('symbiflow-fasm2bels-tcl.j2',
                                 'fasm2bels.tcl',
                                 tcl_params)
            self.render_template('vivado-sh.j2',
                                 'vivado.sh',
                                 dict(tcl="fasm2bels"))

        seed = self.tool_options.get('seed', None)

        commands = EdaCommands()

        # Add vendor variables
        commands.add_var("export EDALIZE_VENDOR=%s" % vendor)
        commands.add_var("export EDALIZE_PART=%s" % part)

        # Synthesis
        targets = self.toplevel + ".eblif"
        command = ["symbiflow_synth", "-t", self.toplevel]
        command += ["-v"] + file_list
        command += ["-d", bitstream_device]
        command += pcf_opts if pcf_opts != [] else ['-p']
        command += ['-P', partname]
        if vendor == "quicklogic" and pins_constraints:
            command += pcf_opts
        command += xdc_opts
        commands.add(command, [targets], [])

        # P&R
        eblif_opt = ["-e", self.toplevel + ".eblif"]
        device_opt = ["-d", part + "_" + device_suffix]

        depends = self.toplevel + ".eblif"
        targets = self.toplevel + ".net"
        command = ["symbiflow_pack"] + eblif_opt + device_opt + sdc_opts + vpr_options
        commands.add(command, [targets], [depends])

        depends = self.toplevel + ".net"
        targets = self.toplevel + ".place"
        command = ["symbiflow_place"] + eblif_opt + device_opt
        command += ["-n", depends, "-P", partname]
        command += sdc_opts + pcf_opts + vpr_options
        commands.add(command, [targets], [depends])

        depends = self.toplevel + ".place"
        targets = self.toplevel + ".route"
        command = ["symbiflow_route"] + eblif_opt + device_opt
        command += sdc_opts + vpr_options
        commands.add(command, [targets], [depends])

        depends = self.toplevel + ".route"
        targets = self.toplevel + ".fasm"
        command = ["symbiflow_write_fasm"] + eblif_opt + device_opt
        command += sdc_opts + vpr_options
        commands.add(command, [targets], [depends])

        depends = self.toplevel + ".fasm"
        targets = self.toplevel + ".bit"
        command = ["symbiflow_write_bitstream"] + ["-d", bitstream_device]
        command += ["-f", depends]
        command += ["-p" if vendor == "xilinx" else "-P", partname]
        command += ["-b", targets]
        commands.add(command, [targets], [depends])

        if fasm2bels:
            targets = self.toplevel+".bit.v"
            command = ['python -m fasm2bels']
            command += ['--db_root', dbroot+f'/{bitstream_device}']
            command += ['--part', partname]
            command += ['--bitread bitread']
            command += ['--bit_file', self.toplevel+'.bit']
            command += ['--fasm_file', self.toplevel+'.bit.fasm']
            command += ['--eblif', self.toplevel+'.eblif']
            command += ['--connection_database channels.db']
            command += ['--rr_graph', rr_graph]
            command += ['--route_file', self.toplevel+".route"]
            command += ['--vpr_grid_map', vpr_grid]
            command += ['--vpr_capnp_schema_dir', vpr_capnp_schema]
            if len(pins_constraints) > 0:
                command += [f"--pcf {' '.join(pins_constraints)}"]
            command += ['--verilog_file', self.toplevel+".bit.v"]
            command += ['--xdc_file', self.toplevel+".bit.xdc"]
            command += ["&& rm channels.db"]
            commands.add(command, [targets], [])
            depends = targets
            targets = "timing_summary.rpt"
            command = ["bash vivado.sh"]
            commands.add(command, [targets], [depends])

        if vendor == "quicklogic":
            depends = self.toplevel + ".bit"
            targets = self.toplevel + ".bin"
            command = ["symbiflow_write_binary"]
            command += [depends]
            command += [targets]
            commands.add(command, [targets], [depends])

            depends = self.toplevel + ".bit"
            targets = self.toplevel + ".h"
            command = ["symbiflow_write_bitheader"]
            command += [depends]
            command += [targets]
            commands.add(command, [targets], [depends])

            depends = self.toplevel + ".bit"
            targets = self.toplevel + ".openocd.cfg"
            command = ["symbiflow_write_openocd"]
            command += [depends]
            command += [targets]
            commands.add(command, [targets], [depends])

            depends = self.toplevel + ".bit"
            targets = self.toplevel + ".jlink"
            command = ["symbiflow_write_jlink"]
            command += [depends]
            command += [targets]
            commands.add(command, [targets], [depends])

            commands.set_default_target(
                self.toplevel
                + ".bin"
                + " "
                + self.toplevel
                + ".h"
                + " "
                + self.toplevel
                + ".openocd.cfg"
            )
        else:
            commands.set_default_target(targets)
        commands.write(os.path.join(self.work_root, "Makefile"))

    def configure_main(self):
        if self.tool_options.get("pnr") == "nextpnr":
            self.configure_nextpnr()
        elif self.tool_options.get("pnr") in ["vtr", "vpr"]:
            self.configure_vpr()
        else:
            logger.error(
                "Unsupported PnR tool: {}".format(self.tool_options.get("pnr"))
            )

    def run_main(self):
        logger.info("Programming")
