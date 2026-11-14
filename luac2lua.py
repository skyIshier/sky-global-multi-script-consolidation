#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lua 5.2 luac Decompiler / Compiler (single-script Python port)

功能：
  1. 反编译客户端格式 .luac 为 .lua 源码
  2. 使用官方 Lua 5.2 luac 作为前端编译 .lua 为客户端格式 .luac
  3. 支持单个文件转换和目录批量转换
  4. 内置终端交互式文字界面 (TUI)

用法：
  python lua_luac_tool.py
  python lua_luac_tool.py decompile in.luac out.lua
  python lua_luac_tool.py compile in.lua out.luac --compiler /path/to/luac
  python lua_luac_tool.py batch-decompile <dir> [-o <out_dir>] [-r] [--pattern *.luac]
  python lua_luac_tool.py batch-compile   <dir> [-o <out_dir>] [-r] [-c <luac>] [--pattern *.lua]
"""

import os
import sys
import re
import json
import math
import struct
import shutil
import subprocess
import tempfile
from pathlib import Path


# ============================================================
# 常量
# ============================================================

LUA_SIGNATURE = bytes([0x1b, 0x4c, 0x75, 0x61])

CLIENT_OPCODE_NAMES = {
    0: 'DIV', 1: 'MOD', 2: 'POW', 3: 'ADD', 4: 'SUB', 5: 'MUL',
    6: 'MOVE', 7: 'LOADK', 8: 'LOADKX', 9: 'LOADBOOL', 10: 'LOADNIL',
    11: 'GETUPVAL', 12: 'GETTABUP', 13: 'GETTABLE', 14: 'SETTABUP',
    15: 'SETUPVAL', 16: 'SETTABLE', 17: 'NEWTABLE', 18: 'SELF',
    19: 'UNM', 20: 'NOT', 21: 'LEN', 22: 'CONCAT', 23: 'JMP',
    24: 'EQ', 25: 'LT', 26: 'LE', 27: 'TEST', 28: 'TESTSET',
    29: 'CALL', 30: 'TAILCALL', 31: 'RETURN', 32: 'FORLOOP',
    33: 'FORPREP', 34: 'TFORCALL', 35: 'TFORLOOP', 36: 'SETLIST',
    37: 'CLOSURE', 38: 'VARARG', 39: 'EXTRAARG',
}

STANDARD_OPCODE_NAMES = {
    0: 'MOVE', 1: 'LOADK', 2: 'LOADKX', 3: 'LOADBOOL', 4: 'LOADNIL',
    5: 'GETUPVAL', 6: 'GETTABUP', 7: 'GETTABLE', 8: 'SETTABUP',
    9: 'SETUPVAL', 10: 'SETTABLE', 11: 'NEWTABLE', 12: 'SELF',
    13: 'ADD', 14: 'SUB', 15: 'MUL', 16: 'DIV', 17: 'MOD', 18: 'POW',
    19: 'UNM', 20: 'NOT', 21: 'LEN', 22: 'CONCAT', 23: 'JMP',
    24: 'EQ', 25: 'LT', 26: 'LE', 27: 'TEST', 28: 'TESTSET',
    29: 'CALL', 30: 'TAILCALL', 31: 'RETURN', 32: 'FORLOOP',
    33: 'FORPREP', 34: 'TFORCALL', 35: 'TFORLOOP', 36: 'SETLIST',
    37: 'CLOSURE', 38: 'VARARG', 39: 'EXTRAARG',
}

OPCODE_BY_NAME = {name: code for code, name in CLIENT_OPCODE_NAMES.items()}
CONSTANT_TAGS = {'NIL': 0, 'BOOLEAN': 1, 'NUMBER': 3, 'STRING': 4}


# ============================================================
# 二进制读写
# ============================================================

class BinaryReader:
    def __init__(self, buffer):
        self.buffer = buffer
        self.offset = 0

    def _assert(self, n):
        if self.offset + n > len(self.buffer):
            raise RuntimeError(f"EOF at offset {self.offset}")

    def read_byte(self):
        self._assert(1)
        v = self.buffer[self.offset]; self.offset += 1
        return v

    def read_int32(self):
        self._assert(4)
        v = struct.unpack_from('<i', self.buffer, self.offset)[0]
        self.offset += 4
        return v

    def read_uint32(self):
        self._assert(4)
        v = struct.unpack_from('<I', self.buffer, self.offset)[0]
        self.offset += 4
        return v

    def read_lua_number(self):
        self._assert(8)
        v = struct.unpack_from('<d', self.buffer, self.offset)[0]
        self.offset += 8
        return v

    def read_bytes(self, n):
        self._assert(n)
        v = self.buffer[self.offset:self.offset + n]
        self.offset += n
        return v

    def read_string(self):
        size = self.read_uint32()
        if size == 0:
            return None
        length = size - 1
        self._assert(length + 1)
        v = self.buffer[self.offset:self.offset + length].decode('utf-8', errors='replace')
        self.offset += length; self.read_byte()
        return v


class BinaryWriter:
    def __init__(self):
        self.chunks = []

    def write_byte(self, v): self.chunks.append(struct.pack('<B', v & 0xff))
    def write_int32(self, v): self.chunks.append(struct.pack('<i', v))
    def write_uint32(self, v): self.chunks.append(struct.pack('<I', v & 0xffffffff))
    def write_lua_number(self, v): self.chunks.append(struct.pack('<d', v))
    def write_bytes(self, data): self.chunks.append(bytes(data))

    def write_lua_string32(self, v):
        if v is None:
            self.write_uint32(0); return
        data = str(v).encode('utf-8')
        self.write_uint32(len(data) + 1)
        self.write_bytes(data)
        self.write_byte(0)

    def to_bytes(self): return b''.join(self.chunks)


# ============================================================
# Lua 格式化
# ============================================================

def escape_lua_string(value):
    return (str(value).replace('\\', '\\\\').replace('"', '\\"')
            .replace('\n', '\\n').replace('\r', '\\r')
            .replace('\t', '\\t').replace('\0', '\\0'))


def lua_string(value):
    return 'nil' if value is None else f'"{escape_lua_string(value)}"'


def lua_key(value):
    if value is None: return 'nil'
    normalized = value
    if isinstance(normalized, str) and normalized.startswith('"') and normalized.endswith('"'):
        try: normalized = json.loads(normalized)
        except Exception: pass
    if isinstance(normalized, str) and re.match(r'^[A-Za-z_][A-Za-z_0-9]*$', normalized):
        return normalized
    return f'["{escape_lua_string(normalized)}"]'


def lua_number(value):
    if isinstance(value, float):
        if math.isinf(value): return '1/0' if value > 0 else '-1/0'
        if math.isnan(value): return '0/0'
        if value.is_integer() and abs(value) < 1e21: return str(int(value))
        return repr(value)
    return str(value)


def is_identifier(value):
    return isinstance(value, str) and re.match(r'^[A-Za-z_][A-Za-z_0-9]*$', value) is not None


# ============================================================
# 指令解码
# ============================================================

class Instruction:
    __slots__ = ('opcode', 'name', 'A', 'B', 'C', 'Bx', 'sBx', 'Ax')

    def __init__(self, opcode, name, A, B, C, Bx, sBx, Ax):
        self.opcode = opcode; self.name = name
        self.A = A; self.B = B; self.C = C
        self.Bx = Bx; self.sBx = sBx; self.Ax = Ax


class InstructionDecoder:
    def decode(self, raw):
        opcode = raw & 0x3f
        A = (raw >> 6) & 0xff
        C = (raw >> 14) & 0x1ff
        B = (raw >> 23) & 0x1ff
        Bx = (raw >> 14) & 0x3ffff
        sBx = Bx - 0x1ffff
        Ax = (raw >> 6) & 0x3ffffff
        return Instruction(opcode, CLIENT_OPCODE_NAMES.get(opcode, f'OP_{opcode}'),
                           A, B, C, Bx, sBx, Ax)


# ============================================================
# 表构建器
# ============================================================

class TableBuilder:
    def __init__(self):
        self.hash_parts = []
        self.array_parts = []

    def add_hash_entry(self, key, value):
        self.hash_parts.append({'key': key, 'value': value})

    def add_array_items(self, start_index, items):
        for i, item in enumerate(items):
            pos = start_index - 1 + i
            while len(self.array_parts) <= pos:
                self.array_parts.append(None)
            self.array_parts[pos] = item

    def build(self):
        array_items = [it for it in self.array_parts if it is not None]
        hash_items = [f"{lua_key(e['key'])} = {e['value']}" for e in self.hash_parts]
        if not array_items and not hash_items:
            return '{}'
        if not array_items and self._should_use_pretty_object_layout():
            return self._build_pretty_object_table(hash_items)
        if array_items:
            return self._build_array_table(array_items, hash_items)
        if self._should_use_multiline_hash_layout():
            return self._build_multiline_hash_table(hash_items)
        return self._build_hash_only_table(hash_items)

    def _build_array_table(self, array_items, hash_items):
        parts = [self._indent_multiline_item(self._compact_simple_array_table(it))
                 for it in array_items]
        parts += [self._indent_multiline_item(it) for it in hash_items]
        return '{\n' + '\n'.join(parts) + '\n}'

    def _build_hash_only_table(self, hash_items):
        return '{ ' + ', '.join(hash_items) + ' }'

    def _build_multiline_hash_table(self, hash_items):
        body = ['  ' + self._indent_nested_hash_value(it) + ',' for it in hash_items]
        return '{\n' + '\n'.join(body) + '\n}'

    def _indent_multiline_item(self, item):
        text = str(item); lines = text.split('\n')
        if len(lines) == 1: return '  ' + text + ','
        first = lines[0]; rest = ['  ' + l for l in lines[1:]]
        indented = ['  ' + first] + rest
        indented[-1] = indented[-1] + ','
        return '\n'.join(indented)

    def _should_use_pretty_object_layout(self):
        sig = ','.join(str(e['key']) for e in self.hash_parts)
        return sig in ('name,bundle,realm', 'name,auto,prio,next,text,prereq')

    def _should_use_multiline_hash_layout(self):
        if any(self._is_multiline_value(e['value']) for e in self.hash_parts):
            return True
        if len(self.hash_parts) >= 4 and self._is_nested_table_map():
            return True
        return len(self.hash_parts) >= 2 and self._is_flag_config_table()

    def _build_pretty_object_table(self, hash_items):
        body = '\n'.join('  ' + it + ',' for it in hash_items)
        return '{\n' + body + '\n}'

    def _compact_simple_array_table(self, item):
        text = str(item); trimmed = text.strip()
        if not trimmed.startswith('{') or not trimmed.endswith('}'):
            return text
        lines = trimmed.split('\n')
        if len(lines) < 3: return text
        middle = [l.strip().rstrip(',') for l in lines[1:-1]]
        if not middle or len(middle) > 4: return text
        if any('=' in l or '{' in l or '}' in l for l in middle): return text
        return '{ ' + ', '.join(middle) + ' }'

    def _indent_nested_hash_value(self, item):
        text = str(item)
        if not self._is_multiline_value(text): return text
        lines = text.split('\n')
        return '\n'.join([lines[0]] + ['  ' + l for l in lines[1:]])

    def _is_multiline_value(self, value): return '\n' in str(value)
    def _is_nested_table_map(self):
        return all(self._is_table_literal(e['value']) for e in self.hash_parts)
    def _is_flag_config_table(self):
        return all(re.match(r'^k[A-Z0-9_]', str(e['key'])) is not None for e in self.hash_parts)
    def _is_table_literal(self, value):
        text = str(value).strip()
        return text.startswith('{') and text.endswith('}')


# ============================================================
# 客户端格式解析器
# ============================================================

class LuacParser:
    def parse(self, buffer):
        reader = BinaryReader(buffer)
        sig = reader.read_bytes(4)
        if sig != LUA_SIGNATURE:
            raise RuntimeError('Invalid Lua bytecode signature')
        self._skip_header(reader)
        return self._parse_function(reader, None)

    def _skip_header(self, reader):
        for _ in range(8): reader.read_byte()
        reader.read_bytes(6)

    def _parse_function(self, reader, parent_source):
        linedefined = reader.read_int32()
        lastlinedefined = reader.read_int32()
        num_params = reader.read_byte()
        is_vararg = reader.read_byte()
        max_stack_size = reader.read_byte()

        num_instructions = reader.read_int32()
        instructions = [reader.read_uint32() for _ in range(num_instructions)]

        num_constants = reader.read_int32()
        constants = [self._read_constant(reader) for _ in range(num_constants)]

        num_protos = reader.read_int32()
        protos = [self._parse_function(reader, None) for _ in range(num_protos)]

        num_upvalues = reader.read_int32()
        upvalues = [{'instack': reader.read_byte(),
                     'idx': reader.read_byte(), 'name': None}
                    for _ in range(num_upvalues)]

        source = reader.read_string()

        num_line_info = reader.read_int32()
        line_info = [reader.read_int32() for _ in range(num_line_info)]

        num_local_vars = reader.read_int32()
        local_vars = [{'name': reader.read_string(),
                       'startPC': reader.read_int32(),
                       'endPC': reader.read_int32()}
                      for _ in range(num_local_vars)]

        num_upvalue_names = reader.read_int32()
        for i in range(num_upvalue_names):
            name = reader.read_string()
            if i < len(upvalues): upvalues[i]['name'] = name

        return {
            'source': source or parent_source,
            'linedefined': linedefined, 'lastlinedefined': lastlinedefined,
            'isVararg': is_vararg, 'numParams': num_params,
            'maxStackSize': max_stack_size, 'instructions': instructions,
            'constants': constants, 'protos': protos, 'upvalues': upvalues,
            'localVars': local_vars, 'lineInfo': line_info,
        }

    def _read_constant(self, reader):
        tag = reader.read_byte()
        if tag == CONSTANT_TAGS['NIL']: return {'type': 'nil', 'value': None}
        if tag == CONSTANT_TAGS['BOOLEAN']: return {'type': 'boolean', 'value': reader.read_byte() != 0}
        if tag == CONSTANT_TAGS['NUMBER']: return {'type': 'number', 'value': reader.read_lua_number()}
        if tag == CONSTANT_TAGS['STRING']: return {'type': 'string', 'value': reader.read_string()}
        raise RuntimeError(f'Unknown constant tag: {tag}')


# ============================================================
# 标准 Lua 5.2 解析器
# ============================================================

class StandardLuacParser:
    def parse(self, buffer):
        reader = BinaryReader(buffer)
        sig = reader.read_bytes(4)
        if sig != LUA_SIGNATURE:
            raise RuntimeError('Invalid standard Lua bytecode signature')
        header = self._read_header(reader)
        if header['version'] != 0x52:
            raise RuntimeError(f"Unsupported standard Lua version: 0x{header['version']:02x}")
        func = self._parse_function(reader, header, None)
        return {'header': header, 'func': func}

    def _read_header(self, reader):
        version = reader.read_byte(); fmt = reader.read_byte()
        endianness = reader.read_byte(); int_size = reader.read_byte()
        size_t_size = reader.read_byte(); instruction_size = reader.read_byte()
        lua_number_size = reader.read_byte(); integral_flag = reader.read_byte()
        tail = reader.read_bytes(6)

        if endianness != 1: raise RuntimeError(f'Unsupported standard Lua endianness flag: {endianness}')
        if int_size != 4: raise RuntimeError(f'Unsupported standard Lua int size: {int_size}')
        if instruction_size != 4: raise RuntimeError(f'Unsupported standard Lua instruction size: {instruction_size}')
        if lua_number_size != 8: raise RuntimeError(f'Unsupported standard Lua number size: {lua_number_size}')
        if integral_flag != 0: raise RuntimeError('Integral standard Lua numbers are not supported')

        return {'version': version, 'format': fmt, 'endianness': endianness,
                'intSize': int_size, 'sizeTSize': size_t_size,
                'instructionSize': instruction_size, 'luaNumberSize': lua_number_size,
                'integralFlag': integral_flag, 'tailBytes': tail}

    def _parse_function(self, reader, header, parent_source):
        linedefined = reader.read_int32(); lastlinedefined = reader.read_int32()
        num_params = reader.read_byte(); is_vararg = reader.read_byte()
        max_stack_size = reader.read_byte()

        num_instructions = reader.read_int32()
        instructions = [self._remap_instruction(reader.read_uint32())
                        for _ in range(num_instructions)]

        num_constants = reader.read_int32()
        constants = [self._read_constant(reader, header) for _ in range(num_constants)]

        num_protos = reader.read_int32()
        protos = [self._parse_function(reader, header, None) for _ in range(num_protos)]

        num_upvalues = reader.read_int32()
        upvalues = [{'instack': reader.read_byte(),
                     'idx': reader.read_byte(), 'name': None}
                    for _ in range(num_upvalues)]

        source = self._read_lua_string(reader, header['sizeTSize'])

        num_line_info = reader.read_int32()
        line_info = [reader.read_int32() for _ in range(num_line_info)]

        num_local_vars = reader.read_int32()
        local_vars = [{'name': self._read_lua_string(reader, header['sizeTSize']),
                       'startPC': reader.read_int32(),
                       'endPC': reader.read_int32()}
                      for _ in range(num_local_vars)]

        num_upvalue_names = reader.read_int32()
        for i in range(num_upvalue_names):
            name = self._read_lua_string(reader, header['sizeTSize'])
            if i < len(upvalues): upvalues[i]['name'] = name

        return {'source': source or parent_source,
                'linedefined': linedefined, 'lastlinedefined': lastlinedefined,
                'numParams': num_params, 'isVararg': is_vararg,
                'maxStackSize': max_stack_size, 'instructions': instructions,
                'constants': constants, 'protos': protos, 'upvalues': upvalues,
                'localVars': local_vars, 'lineInfo': line_info}

    def _read_constant(self, reader, header):
        tag = reader.read_byte()
        if tag == CONSTANT_TAGS['NIL']: return {'type': 'nil', 'value': None}
        if tag == CONSTANT_TAGS['BOOLEAN']: return {'type': 'boolean', 'value': reader.read_byte() != 0}
        if tag == CONSTANT_TAGS['NUMBER']: return {'type': 'number', 'value': reader.read_lua_number()}
        if tag == CONSTANT_TAGS['STRING']:
            return {'type': 'string', 'value': self._read_lua_string(reader, header['sizeTSize'])}
        raise RuntimeError(f'Unknown standard Lua constant tag: {tag}')

    def _read_lua_string(self, reader, size_t_size):
        size = self._read_size_t(reader, size_t_size)
        if size == 0: return None
        length = size - 1
        value = reader.read_bytes(length).decode('utf-8', errors='replace')
        reader.read_byte()
        return value

    def _read_size_t(self, reader, size_t_size):
        if size_t_size == 4: return reader.read_uint32()
        if size_t_size == 8:
            low = reader.read_uint32(); high = reader.read_uint32()
            return (high << 32) | low
        raise RuntimeError(f'Unsupported standard Lua size_t size: {size_t_size}')

    def _remap_instruction(self, raw):
        std_op = raw & 0x3f
        name = STANDARD_OPCODE_NAMES.get(std_op)
        if not name: raise RuntimeError(f'Unknown standard Lua opcode: {std_op}')
        custom = OPCODE_BY_NAME.get(name)
        if custom is None: raise RuntimeError(f'Opcode {name} is missing from client opcode map')
        return (raw & ~0x3f) | custom


# ============================================================
# 客户端格式写入器
# ============================================================

class ClientLuacWriter:
    def write(self, func):
        writer = BinaryWriter()
        self._write_header(writer)
        self._write_function(writer, func)
        return writer.to_bytes()

    def _write_header(self, writer):
        writer.write_bytes(LUA_SIGNATURE)
        for b in (0x52, 0x00, 0x01, 0x04, 0x08, 0x04, 0x08, 0x00):
            writer.write_byte(b)
        writer.write_bytes(bytes([0x19, 0x93, 0x0d, 0x0a, 0x1a, 0x0a]))

    def _write_function(self, writer, func):
        writer.write_int32(func.get('linedefined') or 0)
        writer.write_int32(func.get('lastlinedefined') or 0)
        writer.write_byte(func.get('numParams') or 0)
        writer.write_byte(func.get('isVararg') or 0)
        writer.write_byte(func.get('maxStackSize') or 2)

        instrs = func.get('instructions') or []
        writer.write_int32(len(instrs))
        for instr in instrs: writer.write_uint32(instr)

        consts = func.get('constants') or []
        writer.write_int32(len(consts))
        for c in consts: self._write_constant(writer, c)

        protos = func.get('protos') or []
        writer.write_int32(len(protos))
        for p in protos: self._write_function(writer, p)

        upvalues = func.get('upvalues') or []
        writer.write_int32(len(upvalues))
        for uv in upvalues:
            writer.write_byte(uv.get('instack') or 0)
            writer.write_byte(uv.get('idx') or 0)

        writer.write_lua_string32(func.get('source') or None)

        line_info = func.get('lineInfo') or []
        writer.write_int32(len(line_info))
        for ln in line_info: writer.write_int32(ln)

        local_vars = func.get('localVars') or []
        writer.write_int32(len(local_vars))
        for lv in local_vars:
            writer.write_lua_string32(lv.get('name') or None)
            writer.write_int32(lv.get('startPC') or 0)
            writer.write_int32(lv.get('endPC') or 0)

        writer.write_int32(len(upvalues))
        for uv in upvalues: writer.write_lua_string32(uv.get('name') or None)

    def _write_constant(self, writer, constant):
        t = constant.get('type')
        if t == 'nil': writer.write_byte(CONSTANT_TAGS['NIL'])
        elif t == 'boolean':
            writer.write_byte(CONSTANT_TAGS['BOOLEAN'])
            writer.write_byte(1 if constant.get('value') else 0)
        elif t == 'number':
            writer.write_byte(CONSTANT_TAGS['NUMBER'])
            writer.write_lua_number(constant.get('value'))
        elif t == 'string':
            writer.write_byte(CONSTANT_TAGS['STRING'])
            writer.write_lua_string32(constant.get('value'))
        else: raise RuntimeError(f'Unsupported client constant type: {t}')


# ============================================================
# 函数反编译器
# ============================================================

class FunctionDecompiler:
    def __init__(self, func, options=None):
        options = options or {}
        self.func = func
        self.indent = options.get('indent', 0)
        self.decoder = options.get('decoder') or InstructionDecoder()
        self.lines = []
        self.registers = [None] * (func.get('maxStackSize') or 0)
        self.declared_locals = set()
        self.suppressed_labels = set()
        self.structured_ranges = []
        self.local_scope_stack = []
        self.label_targets = self._collect_label_targets()
        for i in range(func.get('numParams') or 0):
            lvs = func.get('localVars') or []
            if i < len(lvs) and lvs[i].get('name'):
                self._declare_local(lvs[i]['name'])

    def decompile(self):
        special = self._try_special_case_decompile()
        if special is not None: return special
        self._decompile_range(0, len(self.func['instructions']))
        self._flush_open_locals()
        return self.lines

    def _decompile_range(self, start_pc, end_pc):
        instructions = self.func['instructions']
        pc = start_pc
        while pc < end_pc:
            self._emit_label_if_needed(pc)
            inst = self.decoder.decode(instructions[pc])
            n = inst.name
            if n == 'GETTABUP': pc = self._handle_get_tab_up(inst, pc)
            elif n == 'LOADKX': pc = self._handle_load_kx(inst, pc, instructions)
            elif n == 'LOADK':
                self._set_register(inst.A, self._get_constant_expression(inst.Bx))
                self._emit_register_assignment(inst.A, pc); pc += 1
            elif n == 'CALL': pc = self._handle_call(inst, pc)
            elif n == 'NEWTABLE':
                self._set_register(inst.A, TableBuilder()); pc += 1
            elif n == 'SETTABLE': pc = self._handle_set_table(inst, pc)
            elif n == 'SETLIST': pc = self._handle_set_list(inst, pc, instructions)
            elif n == 'RETURN': pc = self._handle_return(inst, pc)
            elif n == 'SETTABUP': pc = self._handle_set_tab_up(inst, pc)
            elif n == 'MOVE':
                self._set_register(inst.A, self._get_register_expression(inst.B, pc))
                self._emit_register_assignment(inst.A, pc); pc += 1
            elif n == 'LOADBOOL':
                self._set_register(inst.A, 'true' if inst.B != 0 else 'false')
                self._emit_register_assignment(inst.A, pc)
                pc += 2 if inst.C else 1
            elif n == 'LOADNIL':
                for i in range(inst.A, inst.A + inst.B + 1):
                    self._set_register(i, 'nil'); self._emit_register_assignment(i, pc)
                pc += 1
            elif n == 'GETUPVAL':
                self._set_register(inst.A, self._get_upvalue_name(inst.B))
                self._emit_register_assignment(inst.A, pc); pc += 1
            elif n == 'GETTABLE':
                self._set_register(inst.A, self._format_table_access(
                    self._get_register_expression(inst.B, pc),
                    self._get_rk_value(inst.C, pc),
                    self._get_key_constant(inst.C)))
                self._emit_register_assignment(inst.A, pc); pc += 1
            elif n == 'SETUPVAL':
                self._emit(f"{self._get_upvalue_name(inst.B)} = "
                           f"{self._get_register_expression(inst.A, pc)}")
                pc += 1
            elif n == 'SELF': pc = self._handle_self(inst, pc)
            elif n in ('ADD', 'SUB', 'MUL', 'DIV', 'MOD', 'POW'):
                pc = self._handle_binary_operation(inst, pc)
            elif n == 'UNM':
                self._set_register(inst.A, f"-{self._get_rk_value(inst.B, pc)}")
                self._emit_register_assignment(inst.A, pc); pc += 1
            elif n == 'NOT':
                self._set_register(inst.A, f"not {self._get_rk_value(inst.B, pc)}")
                self._emit_register_assignment(inst.A, pc); pc += 1
            elif n == 'LEN':
                self._set_register(inst.A, f"#{self._get_rk_value(inst.B, pc)}")
                self._emit_register_assignment(inst.A, pc); pc += 1
            elif n == 'CONCAT': pc = self._handle_concat(inst, pc)
            elif n == 'JMP': pc = self._handle_jump(inst, pc, instructions)
            elif n == 'TEST': pc = self._handle_test(inst, pc, instructions)
            elif n == 'FORLOOP': pc = self._handle_for_loop(inst, pc)
            elif n == 'TFORCALL': pc = self._handle_t_for_call(inst, pc)
            elif n == 'TFORLOOP': pc = self._handle_t_for_loop(inst, pc)
            elif n == 'TESTSET': pc = self._handle_test_set(inst, pc, instructions)
            elif n in ('EQ', 'LT', 'LE'): pc = self._handle_comparison(inst, pc, instructions)
            elif n == 'VARARG': pc = self._handle_vararg(inst, pc)
            elif n == 'EXTRAARG': pc += 1
            elif n == 'FORPREP': pc = self._handle_for_prep(inst, pc, instructions)
            elif n == 'CLOSURE': pc = self._handle_closure(inst, pc)
            elif n == 'TAILCALL': pc = self._handle_tail_call(inst, pc)
            else:
                self._emit(f"-- {inst.name} A={inst.A} B={inst.B} C={inst.C}")
                pc += 1

    # ---- handlers ----

    def _handle_jump(self, inst, pc, instructions):
        target_pc = self._resolve_jump_target(pc, inst.sBx)
        prev = self.decoder.decode(instructions[pc - 1]) if pc > 0 else None
        enclosing_generic_for = self._find_enclosing_structured_range('generic_for')
        if prev and prev.name in ('RETURN', 'TAILCALL'): return pc + 1
        if target_pc > pc:
            next_pc = self._try_emit_generic_for_from_jump(pc, target_pc, instructions)
            if next_pc is not None: return next_pc
            if enclosing_generic_for and target_pc == enclosing_generic_for['endPc']:
                self._flush_open_locals(); self.suppressed_labels.add(target_pc)
                self._emit('break'); return pc + 1
        if (target_pc <= pc
                and not self._is_loop_back_edge_owned_by_structure(pc, target_pc, prev)
                and not self._is_pc_inside_structured_range(pc)):
            condition = self._infer_while_condition(pc, instructions, target_pc)
            if condition:
                self._flush_open_locals(); self._emit(f"while {condition} do")
                self.indent += 1; self.suppressed_labels.add(target_pc)
                self._with_structured_range(target_pc, pc + 1,
                    lambda: self._decompile_range(target_pc + 2, pc))
                self.indent -= 1; self._emit('end'); return pc + 1
        self._flush_open_locals(); self._emit(f"goto {self._label_name(target_pc)}")
        return pc + 1

    def _handle_comparison(self, inst, pc, instructions):
        op_map = {'EQ': '==', 'LT': '<', 'LE': '<='}
        cond = f"{self._get_rk_value(inst.B, pc)} {op_map[inst.name]} {self._get_rk_value(inst.C, pc)}"
        return self._handle_conditional_next(pc, instructions, cond, inst.A != 0)

    def _handle_test(self, inst, pc, instructions):
        source = self._get_register_expression(inst.A, pc)
        return self._handle_conditional_next(pc, instructions, source, inst.C != 0)

    def _handle_test_set(self, inst, pc, instructions):
        source = self._get_register_expression(inst.B, pc)
        local_name = self._get_local_name(inst.A, pc)
        if local_name:
            assignment = (f"{local_name} = {source}" if self._is_local_declared(local_name)
                          else f"local {local_name} = {source}")
            self._declare_local(local_name)
        else:
            self._set_register(inst.A, source); assignment = f"R{inst.A} = {source}"
        next_inst = self.decoder.decode(instructions[pc + 1]) if pc + 1 < len(instructions) else None
        if next_inst and next_inst.name == 'JMP':
            target_pc = self._resolve_jump_target(pc + 1, next_inst.sBx)
            cond = self._conditional_expression(source, inst.C != 0)
            self._flush_open_locals(); self._emit(f"if {cond} then")
            self.indent += 1; self._emit(assignment)
            self._emit(f"goto {self._label_name(target_pc)}")
            self.indent -= 1; self._emit('end'); return pc + 2
        self._flush_open_locals()
        self._emit(f"if {self._conditional_expression(source, inst.C != 0)} then {assignment} end")
        return pc + 1

    def _handle_for_prep(self, inst, pc, instructions):
        loop_var = self._get_numeric_for_variable_name(inst.A, pc)
        init_expr = self._get_register_expression(inst.A, pc)
        limit_expr = self._get_register_expression(inst.A + 1, pc)
        step_expr = self._get_register_expression(inst.A + 2, pc)
        loop_end_pc = self._resolve_jump_target(pc, inst.sBx)
        loop_body_pc = pc + 1
        loop_inst = (self.decoder.decode(instructions[loop_end_pc])
                     if loop_end_pc < len(instructions) else None)
        if loop_inst and loop_inst.name == 'FORLOOP':
            self._flush_open_locals()
            step_text = '' if step_expr == '1' else f", {step_expr}"
            self._remove_trailing_label(loop_body_pc)
            self._emit(f"for {loop_var} = {init_expr}, {limit_expr}{step_text} do")
            self.indent += 1
            self.suppressed_labels.add(loop_body_pc); self.suppressed_labels.add(loop_end_pc)
            def body():
                self._with_structured_range(loop_body_pc, loop_end_pc + 1,
                    lambda: self._decompile_range(loop_body_pc, loop_end_pc), 'numeric_for')
            self._with_local_scope(body)
            self.indent -= 1; self._emit('end'); return loop_end_pc + 1
        self._flush_open_locals()
        self._emit(f"-- for {loop_var} = {init_expr}, {limit_expr}, {step_expr} "
                   f"(body {self._label_name(loop_body_pc)}, loop {self._label_name(loop_end_pc)})")
        return pc + 1

    def _handle_for_loop(self, inst, pc):
        loop_var = self._get_numeric_for_variable_name(inst.A, pc)
        step_expr = self._get_register_expression(inst.A + 2, pc)
        target_pc = self._resolve_jump_target(pc, inst.sBx)
        self._flush_open_locals()
        self._emit(f"-- {loop_var} = {loop_var} + {step_expr}")
        self._emit(f"goto {self._label_name(target_pc)}")
        return pc + 1

    def _handle_t_for_call(self, inst, pc):
        instructions = self.func['instructions']
        loop_inst = (self.decoder.decode(instructions[pc + 1])
                     if pc + 1 < len(instructions) else None)
        setup_jump_pc = self._find_matching_forward_jump_to(pc)
        body_start_pc = setup_jump_pc + 1 if setup_jump_pc >= 0 else -1
        loop_start_pc = (self._resolve_jump_target(pc + 1, loop_inst.sBx)
                         if loop_inst and loop_inst.name == 'TFORLOOP' else None)
        if (loop_inst and loop_inst.name == 'TFORLOOP'
                and setup_jump_pc >= 0 and body_start_pc >= 0
                and loop_start_pc == setup_jump_pc and body_start_pc <= pc - 1):
            iterator_expr = self._get_register_expression(inst.A, pc)
            state_expr = self._get_register_expression(inst.A + 1, pc)
            control_expr = self._get_register_expression(inst.A + 2, pc)
            loop_exit_pc = pc + 2; body_end_pc = pc
            loop_vars = self._get_generic_for_variables(inst.A, inst.C, body_start_pc)
            variable_list = (', '.join(loop_vars) if loop_vars
                             else self._get_fallback_generic_for_vars(inst.C))
            self._flush_open_locals()
            self._remove_trailing_label(body_start_pc)
            self._emit(f"for {variable_list} in {iterator_expr}, {state_expr}, {control_expr} do")
            self.indent += 1
            for p in (loop_start_pc, body_start_pc, pc, pc + 1):
                self.suppressed_labels.add(p)
            def body():
                self._with_structured_range(loop_start_pc, loop_exit_pc,
                    lambda: self._decompile_range(body_start_pc, body_end_pc), 'generic_for')
            self._with_local_scope(body)
            self.indent -= 1; self._emit('end'); return loop_exit_pc
        return self._emit_generic_t_for_fallback(inst, pc)

    def _emit_generic_t_for_fallback(self, inst, pc):
        iterator_expr = self._get_register_expression(inst.A, pc)
        state_expr = self._get_register_expression(inst.A + 1, pc)
        control_expr = self._get_register_expression(inst.A + 2, pc)
        self._flush_open_locals()
        self._emit(f"-- tforcall {iterator_expr}({state_expr}, {control_expr}) -> {inst.C} result(s)")
        return pc + 1

    def _handle_t_for_loop(self, inst, pc):
        control_expr = self._get_register_expression(inst.A + 1, pc)
        target_pc = self._resolve_jump_target(pc, inst.sBx)
        self._flush_open_locals()
        self._emit(f"if {control_expr} ~= nil then goto {self._label_name(target_pc)} end")
        return pc + 1

    def _handle_conditional_next(self, pc, instructions, raw_condition, execute_when_true):
        next_inst = self.decoder.decode(instructions[pc + 1]) if pc + 1 < len(instructions) else None
        jump_condition = self._conditional_expression(raw_condition, execute_when_true)
        body_condition = self._negate_condition(jump_condition)
        if next_inst and next_inst.name == 'JMP':
            target_pc = self._resolve_jump_target(pc + 1, next_inst.sBx)
            else_pc = pc + 2
            body_inst = (self.decoder.decode(instructions[else_pc])
                         if else_pc < len(instructions) else None)
            body_jump_inst = (self.decoder.decode(instructions[else_pc + 1])
                              if else_pc + 1 < len(instructions) else None)
            fallback_inst = (self.decoder.decode(instructions[target_pc])
                             if target_pc < len(instructions) else None)
            if (body_inst and body_inst.name == 'TESTSET'
                    and body_jump_inst and body_jump_inst.name == 'JMP'
                    and fallback_inst and fallback_inst.name == 'MOVE'
                    and body_inst.A == fallback_inst.A
                    and self._resolve_jump_target(else_pc + 1, body_jump_inst.sBx) == target_pc + 1):
                source = self._get_register_expression(body_inst.B, else_pc)
                fallback = self._get_register_expression(fallback_inst.B, target_pc)
                for p in (target_pc, target_pc + 1,
                          self._resolve_jump_target(else_pc + 1, body_jump_inst.sBx)):
                    self.suppressed_labels.add(p)
                self._set_register(body_inst.A, f"{body_condition} and {source} or {fallback}")
                return target_pc + 1
            if (self._is_inside_generic_for_body() and target_pc > else_pc
                    and self._jumps_to_current_generic_for_guard_tail(target_pc)):
                self._flush_open_locals(); self._emit(f"if {body_condition} then")
                self.indent += 1; self.suppressed_labels.add(else_pc)
                def body():
                    self._with_structured_range(else_pc, target_pc,
                        lambda: self._decompile_range(else_pc, target_pc), 'generic_for_guard')
                self._with_local_scope(body)
                self.indent -= 1; self._emit('end'); return target_pc
            if target_pc > else_pc:
                then_end_pc = target_pc - 1
                tail_inst = (self.decoder.decode(instructions[then_end_pc])
                             if then_end_pc < len(instructions) else None)
                else_target_pc = (self._resolve_jump_target(then_end_pc, tail_inst.sBx)
                                  if tail_inst and tail_inst.name == 'JMP' else None)
                self._flush_open_locals()
                if tail_inst and tail_inst.name == 'JMP' and else_target_pc <= pc:
                    self._remove_trailing_label(pc); self._emit(f"while {body_condition} do")
                    self.indent += 1
                    self.suppressed_labels.add(else_pc); self.suppressed_labels.add(target_pc)
                    def body():
                        self._with_structured_range(else_pc, target_pc,
                            lambda: self._decompile_range(else_pc, then_end_pc), 'while')
                    self._with_local_scope(body)
                    self.indent -= 1; self._emit('end'); return target_pc
                if tail_inst and tail_inst.name == 'JMP' and else_target_pc > target_pc:
                    self._emit(f"if {body_condition} then"); self.indent += 1
                    self.suppressed_labels.add(else_pc); self.suppressed_labels.add(else_target_pc)
                    def then_body():
                        self._with_structured_range(else_pc, else_target_pc,
                            lambda: self._decompile_range(else_pc, then_end_pc), 'if_then')
                    self._with_local_scope(then_body)
                    self.indent -= 1; self._emit('else'); self.indent += 1
                    self.suppressed_labels.add(target_pc)
                    def else_body():
                        self._with_structured_range(target_pc, else_target_pc,
                            lambda: self._decompile_range(target_pc, else_target_pc), 'if_else')
                    self._with_local_scope(else_body)
                    self.indent -= 1; self._emit('end'); return else_target_pc
                self._emit(f"if {body_condition} then"); self.indent += 1
                self.suppressed_labels.add(else_pc); self.suppressed_labels.add(target_pc)
                def body():
                    self._with_structured_range(else_pc, target_pc,
                        lambda: self._decompile_range(else_pc, target_pc), 'if_then')
                self._with_local_scope(body)
                self.indent -= 1; self._emit('end'); return target_pc
            self._flush_open_locals()
            self._emit(f"if {jump_condition} then goto {self._label_name(target_pc)} end")
            return pc + 2
        self._flush_open_locals(); self._emit(f"-- branch when {jump_condition}")
        return pc + 1

    def _handle_get_tab_up(self, inst, pc):
        upvalue_name = self._get_upvalue_name(inst.B)
        key = self._get_rk_value(inst.C, pc); key_const = self._get_key_constant(inst.C)
        self._set_register(inst.A, self._format_upvalue_access(upvalue_name, key, key_const))
        self._emit_register_assignment(inst.A, pc); return pc + 1

    def _handle_load_kx(self, inst, pc, instructions):
        extra = self.decoder.decode(instructions[pc + 1]) if pc + 1 < len(instructions) else None
        if not extra or extra.name != 'EXTRAARG':
            self._set_register(inst.A, f"KX{pc}")
            self._emit_register_assignment(inst.A, pc); return pc + 1
        self._set_register(inst.A, self._get_constant_expression(extra.Ax))
        self._emit_register_assignment(inst.A, pc); return pc + 2

    def _handle_set_table(self, inst, pc):
        table_value = self.registers[inst.A]
        key_const = self._get_key_constant(inst.B)
        key_text = key_const['value'] if key_const and key_const.get('type') == 'string' \
                   else self._get_rk_value(inst.B, pc)
        value_text = self._get_rk_value(inst.C, pc)
        if isinstance(table_value, TableBuilder):
            table_value.add_hash_entry(key_text, value_text)
        else:
            self._emit(f"{self._format_table_access(
                self._get_register_expression(inst.A, pc),
                self._get_rk_value(inst.B, pc),
                key_const)} = {value_text}")
        return pc + 1

    def _handle_set_list(self, inst, pc, instructions):
        base = inst.A; count = inst.B
        if inst.C == 0:
            extra = self.decoder.decode(instructions[pc + 1])
            start_index = extra.Ax; pc += 1
        else:
            start_index = (inst.C - 1) * 50 + 1
        item_count = count if count != 0 else self._count_top_set_list_items(base)
        items = [self._get_register_expression(base + 1 + i, pc) for i in range(item_count)]
        table_value = self.registers[base]
        if isinstance(table_value, TableBuilder):
            table_value.add_array_items(start_index, items)
        else:
            for i in range(item_count):
                self._emit(f"{self._get_register_expression(base, pc)}[{start_index + i}] = {items[i]}")
        return pc + 1

    def _handle_return(self, inst, pc):
        instructions = self.func['instructions']
        prev = self.decoder.decode(instructions[pc - 1]) if pc > 0 else None
        if prev and prev.name == 'TAILCALL': return pc + 1
        if inst.B == 1:
            if pc == len(instructions) - 1: return pc + 1
            if prev and prev.name == 'RETURN': return pc + 1
            self._emit('return'); return pc + 1
        if inst.B == 2:
            local_name = self._get_local_name(inst.A, pc)
            value = self.registers[inst.A]
            if local_name and not self._is_local_declared(local_name) and value is not None:
                self._declare_local(local_name)
                self._emit(f"local {local_name} = {self._value_to_string(value)}")
                self._emit(f"return {local_name}"); return pc + 1
        results = []
        if inst.B == 0:
            for i in range(inst.A, len(self.registers)):
                if self.registers[i] is None: break
                results.append(self._get_register_expression(i, pc))
        else:
            for i in range(inst.B - 1):
                results.append(self._get_register_expression(inst.A + i, pc))
        self._emit(f"return {', '.join(results)}"); return pc + 1

    def _handle_set_tab_up(self, inst, pc):
        upvalue_name = self._get_upvalue_name(inst.A)
        key = self._get_rk_value(inst.B, pc); key_const = self._get_key_constant(inst.B)
        value = self._get_rk_value(inst.C, pc)
        target = self._format_upvalue_assignment(upvalue_name, key, key_const)
        decl = self._try_format_function_declaration(target, value)
        self._emit(decl if decl else f"{target} = {value}")
        return pc + 1

    def _handle_self(self, inst, pc):
        table_expr = self._get_register_expression(inst.B, pc)
        key_expr = self._get_rk_value(inst.C, pc); key_const = self._get_key_constant(inst.C)
        self._set_register(inst.A, {'type': 'method', 'object': table_expr,
            'keyExpression': key_expr, 'keyConstant': key_const,
            'fallback': self._format_table_access(table_expr, key_expr, key_const)})
        self._set_register(inst.A + 1, table_expr)
        return pc + 1

    def _handle_binary_operation(self, inst, pc):
        op_map = {'ADD': '+', 'SUB': '-', 'MUL': '*', 'DIV': '/', 'MOD': '%', 'POW': '^'}
        expr = f"{self._get_rk_value(inst.B, pc)} {op_map[inst.name]} {self._get_rk_value(inst.C, pc)}"
        self._set_register(inst.A, expr); self._emit_register_assignment(inst.A, pc)
        return pc + 1

    def _handle_vararg(self, inst, pc):
        result_count = (len(self.registers) - inst.A) if inst.B == 0 else (inst.B - 1)
        if result_count <= 0:
            self._set_register(inst.A, '...'); return pc + 1
        for i in range(result_count):
            self._set_register(inst.A + i, '...' if i == 0 else f'select({i + 1}, ...)')
            self._emit_register_assignment(inst.A + i, pc)
        return pc + 1

    def _handle_concat(self, inst, pc):
        parts = [self._get_register_expression(i, pc) for i in range(inst.B, inst.C + 1)]
        self._set_register(inst.A, ' .. '.join(parts))
        self._emit_register_assignment(inst.A, pc); return pc + 1

    def _handle_closure(self, inst, pc):
        protos = self.func['protos']
        if inst.Bx >= len(protos): return pc + 1
        proto = protos[inst.Bx]
        params = []
        for i in range(proto.get('numParams') or 0):
            lvs = proto.get('localVars') or []
            params.append(lvs[i].get('name') if i < len(lvs) and lvs[i].get('name')
                          else f'p{i}')
        nested = FunctionDecompiler(proto, {'indent': self.indent + 1, 'decoder': self.decoder})
        nested_lines = nested.decompile()
        if not nested_lines or (len(nested_lines) == 1 and nested_lines[0].strip() == 'return'):
            self._set_register(inst.A, f"function({', '.join(params)}) end")
            return pc + 1
        if (len(nested_lines) >= 2 and nested_lines[0].lstrip().startswith('function ')
                and nested_lines[-1].strip() == 'end'):
            self._set_register(inst.A, self._dedent_function_lines(nested_lines))
            return pc + 1
        body = '\n'.join(nested_lines)
        self._set_register(inst.A,
            f"function({', '.join(params)})\n{body}\n{'  ' * self.indent}end")
        self._emit_register_assignment(inst.A, pc); return pc + 1

    def _handle_tail_call(self, inst, pc):
        func_expr = self._get_register_expression(inst.A, pc)
        arg_count = -1 if inst.B == 0 else inst.B - 1
        args = ['...'] if arg_count < 0 else \
               [self._get_register_expression(inst.A + i, pc) for i in range(1, arg_count + 1)]
        self._flush_open_locals()
        self._emit(f"return {self._format_call_expression(func_expr, args)}")
        return pc + 1

    def _handle_call(self, inst, pc):
        func_register = inst.A; return_count = inst.C
        func_expr = self.registers[func_register]
        args = self._collect_call_arguments(func_register, inst.B)
        if return_count == 1:
            self._flush_open_locals()
            self._emit(self._format_call_expression(func_expr, args))
            self._clear_registers_from(func_register); return pc + 1
        if return_count == 0:
            self._set_register(func_register, {'type': 'call', 'func': func_expr, 'args': args})
            self._clear_registers_from(func_register + 1); return pc + 1
        if return_count == 2:
            self._set_register(func_register, {'type': 'call', 'func': func_expr, 'args': args})
            self._emit_register_assignment(func_register, pc)
            self._clear_registers_from(func_register + 1); return pc + 1
        multi_call = {'type': 'multirescall', 'func': func_expr, 'args': args}
        for i in range(return_count - 1):
            self._set_register(func_register + i,
                {'type': 'multires', 'call': multi_call, 'index': i})
        self._emit_multi_return_assignment(func_register, return_count - 1, pc, multi_call)
        return pc + 1

    def _collect_call_arguments(self, func_register, arg_flag):
        args = []; arg_count = -1 if arg_flag == 0 else arg_flag - 1
        if arg_count < 0:
            for i in range(func_register + 1, len(self.registers)):
                if self.registers[i] is None: break
                args.append(self.registers[i])
            return args
        for i in range(1, arg_count + 1):
            idx = func_register + i
            args.append(self.registers[idx] if idx < len(self.registers) else f"R{idx}")
        return args

    def _emit_multi_return_assignment(self, start_register, count, pc, multi_call):
        local_names = []
        for i in range(count):
            local_name = self._get_local_name(start_register + i, pc)
            if not local_name or self._is_implicit_loop_control_name(local_name): return
            local_names.append(local_name)
        all_declared = all(self._is_local_declared(n) for n in local_names)
        all_undeclared = all(not self._is_local_declared(n) for n in local_names)
        if not all_declared and not all_undeclared: return
        for name in local_names: self._declare_local(name)
        prefix = 'local ' if all_undeclared else ''
        self._emit(f"{prefix}{', '.join(local_names)} = "
                   f"{self._format_call_expression(multi_call['func'], multi_call['args'])}")

    # ---- constants / RK ----

    def _get_constant_expression(self, index):
        constants = self.func['constants']
        if index >= len(constants): return f"K{index}"
        c = constants[index]; t = c.get('type')
        if t == 'string': return lua_string(c['value'])
        if t == 'number': return lua_number(c['value'])
        if t == 'boolean': return 'true' if c['value'] else 'false'
        if t == 'nil': return 'nil'
        return f"K{index}"

    def _get_constant_value(self, index):
        constants = self.func['constants']
        return constants[index] if 0 <= index < len(constants) else None

    def _get_key_constant(self, value):
        return self._get_constant_value(value & 0xff) if value & 0x100 else None

    def _get_rk_value(self, value, pc):
        return (self._get_constant_expression(value & 0xff) if value & 0x100
                else self._get_register_expression(value, pc))

    def _get_register_expression(self, register_index, pc):
        if register_index >= len(self.registers): return f"R{register_index}"
        value = self.registers[register_index]
        local_name = self._get_local_name(register_index, pc)
        if local_name and self._is_local_declared(local_name): return local_name
        if value is not None: return self._value_to_string(value)
        return local_name or f"R{register_index}"

    def _count_top_set_list_items(self, base_register):
        count = 0
        for i in range(base_register + 1, len(self.registers)):
            if self.registers[i] is None: break
            count += 1
        return count

    def _get_local_name(self, register_index, pc):
        for i, lv in enumerate(self.func['localVars']):
            if lv['startPC'] <= pc < lv['endPC'] and i == register_index:
                return lv.get('name')
        return None

    def _get_register_local_name(self, register_index):
        lvs = self.func['localVars']
        return lvs[register_index].get('name') if register_index < len(lvs) else None

    def _get_upvalue_name(self, index):
        uvs = self.func['upvalues']
        return (uvs[index].get('name') or '_ENV') if index < len(uvs) else f"_U{index}"

    def _emit_register_assignment(self, register_index, pc):
        local_name = self._get_local_name(register_index, pc)
        if not local_name or self._is_implicit_loop_control_name(local_name): return
        value = self.registers[register_index]
        if isinstance(value, TableBuilder): return
        if isinstance(value, dict) and value.get('type') == 'method': return
        is_first = not self._is_local_declared(local_name)
        self._declare_local(local_name)
        self._emit(f"{'local ' if is_first else ''}{local_name} = {self._value_to_string(value)}")

    def _set_register(self, index, value):
        if 0 <= index < len(self.registers): self.registers[index] = value

    def _clear_registers_from(self, start_index):
        for i in range(start_index, len(self.registers)): self.registers[i] = None

    # ---- label/jump ----

    def _collect_label_targets(self):
        targets = set(); instructions = self.func['instructions']
        for pc in range(len(instructions)):
            inst = self.decoder.decode(instructions[pc])
            if inst.name in ('JMP', 'FORLOOP', 'FORPREP', 'TFORLOOP'):
                targets.add(self._resolve_jump_target(pc, inst.sBx))
            if inst.name in ('EQ', 'LT', 'LE', 'TEST', 'TESTSET') and pc + 1 < len(instructions):
                next_inst = self.decoder.decode(instructions[pc + 1])
                if next_inst.name == 'JMP':
                    targets.add(self._resolve_jump_target(pc + 1, next_inst.sBx))
        return targets

    def _emit_label_if_needed(self, pc):
        if (pc in self.label_targets and pc not in self.suppressed_labels
                and not self._is_pc_inside_structured_range(pc)):
            self.lines.append(f"{'  ' * self.indent}::{self._label_name(pc)}::")

    def _is_loop_back_edge_owned_by_structure(self, pc, target_pc, prev):
        if not prev: return False
        return ((prev.name == 'FORLOOP' or prev.name == 'TFORLOOP') and target_pc <= pc)

    def _infer_while_condition(self, pc, instructions, target_pc):
        if pc <= target_pc: return None
        guard = (self.decoder.decode(instructions[target_pc])
                 if target_pc < len(instructions) else None)
        guard_jump = (self.decoder.decode(instructions[target_pc + 1])
                      if target_pc + 1 < len(instructions) else None)
        if not guard or not guard_jump or guard_jump.name != 'JMP': return None
        if self._resolve_jump_target(target_pc + 1, guard_jump.sBx) != pc + 1: return None
        if guard.name == 'TEST':
            return self._conditional_expression(
                self._get_register_expression(guard.A, target_pc), guard.C != 0)
        if guard.name in ('EQ', 'LT', 'LE'):
            op_map = {'EQ': '==', 'LT': '<', 'LE': '<='}
            return self._conditional_expression(
                f"{self._get_rk_value(guard.B, target_pc)} {op_map[guard.name]} "
                f"{self._get_rk_value(guard.C, target_pc)}", guard.A != 0)
        return None

    def _with_structured_range(self, start_pc, end_pc, callback, kind='block'):
        self.structured_ranges.append({'startPc': start_pc, 'endPc': end_pc, 'kind': kind})
        try: callback()
        finally: self.structured_ranges.pop()

    def _with_local_scope(self, callback):
        self.local_scope_stack.append(set())
        try: callback()
        finally: self.local_scope_stack.pop()

    def _declare_local(self, name):
        self.declared_locals.add(name)
        if self.local_scope_stack: self.local_scope_stack[-1].add(name)

    def _is_local_declared(self, name): return name in self.declared_locals
    def _is_pc_inside_structured_range(self, pc):
        return any(r['startPc'] <= pc < r['endPc'] for r in self.structured_ranges)

    def _find_enclosing_structured_range(self, kind):
        for r in reversed(self.structured_ranges):
            if r['kind'] == kind: return r
        return None

    def _resolve_jump_target(self, pc, sBx): return pc + 1 + sBx
    def _label_name(self, pc): return f"L{pc}"

    def _conditional_expression(self, expr, execute_when_true):
        return expr if execute_when_true else f"not ({expr})"

    def _negate_condition(self, condition):
        normalized = self._normalize_condition(condition)
        if normalized.startswith('not (') and normalized.endswith(')'):
            return self._normalize_condition(normalized[5:-1])
        return self._normalize_condition(f"not ({normalized})")

    def _normalize_condition(self, condition):
        patterns = [
            (r'^not \((.+) == (.+)\)$', r'\1 ~= \2'),
            (r'^not \((.+) ~= (.+)\)$', r'\1 == \2'),
            (r'^not \((.+) < (.+)\)$', r'\1 >= \2'),
            (r'^not \((.+) <= (.+)\)$', r'\1 > \2'),
            (r'^not \((.+) > (.+)\)$', r'\1 <= \2'),
            (r'^not \((.+) >= (.+)\)$', r'\1 < \2'),
            (r'^not \((not \(.+\))\)$', r'\1'),
        ]
        for pat, repl in patterns:
            if re.match(pat, condition): return re.sub(pat, repl, condition)
        return condition

    def _get_numeric_for_variable_name(self, base_register, pc):
        return (self._get_register_local_name(base_register + 3)
                or self._get_local_name(base_register + 3, pc)
                or self._get_local_name(base_register, pc)
                or f"R{base_register + 3}")

    # ---- value formatting ----

    def _value_to_string(self, value):
        if value is None: return 'nil'
        if isinstance(value, TableBuilder): return value.build()
        if isinstance(value, dict):
            t = value.get('type')
            if t == 'method': return value['fallback']
            if t == 'call': return self._format_call_expression(value['func'], value['args'])
            if t == 'multires':
                call_text = self._format_call_expression(value['call']['func'], value['call']['args'])
                return call_text if value['index'] == 0 else f"select({value['index'] + 1}, {call_text})"
        if isinstance(value, float): return lua_number(value)
        return str(value)

    def _format_call_expression(self, func_expression, args):
        if isinstance(func_expression, dict) and func_expression.get('type') == 'method':
            first_arg = args[0] if args else None
            if self._value_to_string(first_arg) == func_expression['object']:
                method_name = self._resolve_method_name(
                    func_expression['keyExpression'], func_expression['keyConstant'])
                if method_name:
                    arg_text = ', '.join(self._value_to_string(a) for a in args[1:])
                    return f"{func_expression['object']}:{method_name}({arg_text})"
        dsl = self._format_dsl_expression(func_expression, args)
        if dsl: return dsl
        func_text = self._value_to_string(func_expression)
        special = self._format_special_call(func_text, args)
        if special: return special
        args_text = ', '.join(self._value_to_string(a) for a in args)
        return f"{func_text}({args_text})"

    def _format_dsl_expression(self, func_expression, args):
        if isinstance(func_expression, str):
            if not self._is_dsl_callable(func_expression): return None
            formatted = [self._format_dsl_argument(a) for a in args]
            return func_expression + (' ' + ' '.join(formatted) if formatted else '')
        if not (isinstance(func_expression, dict) and func_expression.get('type') == 'call'):
            return None
        outer = self._format_dsl_expression(func_expression['func'], func_expression['args'])
        if not outer: return None
        appended = [self._format_dsl_argument(a) for a in args]
        return outer + (' ' + ' '.join(appended) if appended else '')

    def _format_dsl_argument(self, value):
        if isinstance(value, TableBuilder): return value.build()
        if isinstance(value, str): return value
        if isinstance(value, dict) and value.get('type') == 'call':
            return f"({self._format_call_expression(value['func'], value['args'])})"
        return self._value_to_string(value)

    def _is_dsl_callable(self, expression):
        return isinstance(expression, str) and \
               self._strip_quotes(expression) in ('resource', 'resourcedef')

    def _resolve_method_name(self, key_expression, key_constant):
        if key_constant and key_constant.get('type') == 'string' and is_identifier(key_constant['value']):
            return key_constant['value']
        unquoted = self._strip_quotes(key_expression)
        return unquoted if is_identifier(unquoted) else None

    def _format_upvalue_access(self, upvalue_name, key_expression, key_constant):
        if (upvalue_name == '_ENV' and key_constant
                and key_constant.get('type') == 'string'
                and is_identifier(key_constant['value'])):
            return key_constant['value']
        return self._format_table_access(upvalue_name, key_expression, key_constant)

    def _format_upvalue_assignment(self, upvalue_name, key_expression, key_constant):
        return self._format_upvalue_access(upvalue_name, key_expression, key_constant)

    def _format_table_access(self, base_expression, key_expression, key_constant):
        if (key_constant and key_constant.get('type') == 'string'
                and is_identifier(key_constant['value'])):
            return f"{base_expression}.{key_constant['value']}"
        return f"{base_expression}[{key_expression}]"

    def _strip_quotes(self, value):
        if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
            return value[1:-1]
        return value

    def _try_format_function_declaration(self, target, value):
        if not isinstance(target, str) or not isinstance(value, str): return None
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', target): return None
        trimmed = value.lstrip()
        if not (trimmed.startswith('function(') or trimmed.startswith(f'function {target}(')):
            return None
        if trimmed.startswith(f'function {target}('):
            return self._normalize_multiline_function(trimmed)
        return self._normalize_multiline_function(
            f"function {target}{trimmed[len('function'):]}")

    def _normalize_multiline_function(self, text):
        lines = text.split('\n')
        if len(lines) <= 1: return text
        return '\n'.join([lines[0].lstrip()] + lines[1:])

    def _format_special_call(self, func_text, args):
        if func_text == 'level': return self._format_level_call(args)
        if func_text == 'bundle': return self._format_bundle_call(args)
        return None

    def _format_level_call(self, args):
        values = [self._value_to_string(a) for a in args]
        if len(values) == 2: return f"level( {values[0]}, {values[1]} )"
        if len(values) == 3: return f"level( {values[0]}, {values[1]}, {values[2]} )"
        return f"level( {', '.join(values)} )"

    def _format_bundle_call(self, args):
        values = [self._value_to_string(a) for a in args]
        if len(values) == 6:
            return (f"bundle( {values[0]}, {values[1]}, {values[2]}, {values[3]}, "
                    f"{values[4]}, {self._compact_inline_array_literal(values[5])} )")
        return f"bundle( {', '.join(values)} )"

    def _compact_inline_array_literal(self, text):
        value = str(text).strip()
        if not value.startswith('{') or not value.endswith('}'): return text
        lines = value.split('\n')
        if len(lines) < 3: return text
        middle = [l.strip().rstrip(',') for l in lines[1:-1]]
        if len(middle) > 4 or any('=' in l or '{' in l or '}' in l for l in middle):
            return text
        return '{ ' + ', '.join(middle) + ' }'

    def _dedent_function_lines(self, lines):
        if not isinstance(lines, list) or len(lines) <= 1:
            return '\n'.join(lines) if isinstance(lines, list) else str(lines)
        min_indent = float('inf')
        for i in range(1, len(lines)):
            if not lines[i].strip(): continue
            m = re.match(r'^ *', lines[i])
            min_indent = min(min_indent, len(m.group(0)) if m else 0)
        if not math.isfinite(min_indent) or min_indent <= 0:
            return '\n'.join(lines)
        return '\n'.join([lines[0].lstrip()] + [l[min_indent:] for l in lines[1:]])

    def _flush_open_locals(self):
        for i in range(self.func.get('numParams') or 0, len(self.registers)):
            local_name = self._get_register_local_name(i)
            if not local_name or self._is_local_declared(local_name): continue
            if self._is_implicit_loop_control_name(local_name): continue
            value = self.registers[i]
            if value is None: continue
            if isinstance(value, dict) and value.get('type') == 'method': continue
            self._declare_local(local_name)
            self._emit(f"local {local_name} = {self._value_to_string(value)}")

    def _is_implicit_loop_control_name(self, name):
        return isinstance(name, str) and (name.startswith('(for ') or name.startswith('('))

    # ---- generic for helpers ----

    def _get_generic_for_variables(self, base_register, result_count, pc):
        names = []
        for i in range(result_count):
            name = (self._get_local_name(base_register + 3 + i, pc)
                    or self._get_register_local_name(base_register + 3 + i))
            if name and not self._is_implicit_loop_control_name(name): names.append(name)
        return names

    def _get_fallback_generic_for_vars(self, result_count):
        if not result_count or result_count < 1: return 'v'
        return ', '.join('k' if i == 0 else ('v' if i == 1 else f'v{i}')
                         for i in range(result_count))

    def _try_emit_generic_for_from_jump(self, pc, target_pc, instructions):
        if target_pc >= len(instructions) or target_pc + 1 >= len(instructions): return None
        tfor_call = self.decoder.decode(instructions[target_pc])
        tfor_loop = self.decoder.decode(instructions[target_pc + 1])
        if tfor_call.name != 'TFORCALL' or tfor_loop.name != 'TFORLOOP': return None
        body_start_pc = pc + 1
        if self._resolve_jump_target(target_pc + 1, tfor_loop.sBx) != body_start_pc: return None
        loop_exit_pc = target_pc + 2; body_end_pc = target_pc
        variable_list = self._get_generic_for_variable_list(
            tfor_call.A, tfor_call.C, body_start_pc)
        iterator_expr = self._format_generic_for_iterator_expression(
            tfor_call.A, body_start_pc)
        self._flush_open_locals()
        self._emit(f"for {variable_list} in {iterator_expr} do")
        self.indent += 1
        for p in (body_start_pc, target_pc, target_pc + 1):
            self.suppressed_labels.add(p)
        def body():
            self._with_structured_range(body_start_pc, loop_exit_pc,
                lambda: self._decompile_range(body_start_pc, body_end_pc), 'generic_for')
        self._with_local_scope(body)
        self.indent -= 1; self._emit('end'); return loop_exit_pc

    def _get_generic_for_variable_list(self, base_register, result_count, pc):
        loop_vars = self._get_generic_for_variables(base_register, result_count, pc)
        return ', '.join(loop_vars) if loop_vars else self._get_fallback_generic_for_vars(result_count)

    def _format_generic_for_iterator_expression(self, base_register, pc):
        def get(idx):
            return self.registers[idx] if idx < len(self.registers) else None
        base_value, state_value, control_value = get(base_register), get(base_register + 1), get(base_register + 2)
        if (self._is_multires_index(base_value, 0)
                and self._is_multires_index(state_value, 1, base_value.get('call'))
                and self._is_multires_index(control_value, 2, base_value.get('call'))):
            return self._format_call_expression(
                base_value['call']['func'], base_value['call']['args'])
        return ', '.join([
            self._get_register_expression(base_register, pc),
            self._get_register_expression(base_register + 1, pc),
            self._get_register_expression(base_register + 2, pc)])

    def _is_multires_index(self, value, index, expected_call=None):
        if not (isinstance(value, dict) and value.get('type') == 'multires'): return False
        if value['index'] != index: return False
        return expected_call is None or value['call'] is expected_call

    def _is_inside_generic_for_body(self):
        return bool(self.structured_ranges) and self.structured_ranges[-1]['kind'] == 'generic_for'

    def _jumps_to_current_generic_for_guard_tail(self, target_pc):
        for r in reversed(self.structured_ranges):
            if r['kind'] == 'generic_for':
                return target_pc in (r['endPc'], r['endPc'] - 2, r['endPc'] - 1)
        return False

    def _find_matching_forward_jump_to(self, target_pc):
        instructions = self.func['instructions']
        for pc in range(target_pc - 1, -1, -1):
            inst = self.decoder.decode(instructions[pc])
            if inst.name == 'JMP' and self._resolve_jump_target(pc, inst.sBx) == target_pc:
                return pc
        return -1

    def _emit(self, line):
        prefix = '  ' * self.indent
        for part in str(line).split('\n'):
            self.lines.append(prefix + part)

    def _remove_trailing_label(self, pc):
        expected = f"{'  ' * self.indent}::{self._label_name(pc)}::"
        if self.lines and self.lines[-1] == expected: self.lines.pop()

    # ---- 特例 ----

    def _try_special_case_decompile(self):
        if self._matches_level_cmp_pattern(): return self._decompile_level_cmp_pattern()
        return None

    def _matches_level_cmp_pattern(self):
        if self.func.get('numParams') != 2 or len(self.func['instructions']) != 49:
            return False
        names = [lv.get('name') for lv in self.func['localVars']]
        return all(n in names for n in ['level1', 'level2', 'levelIndex1',
                                        'levelIndex2', 'i', 'val'])

    def _decompile_level_cmp_pattern(self):
        lvs = self.func['localVars']
        get = lambda i, d: (lvs[i].get('name') if i < len(lvs) else None) or d
        l1, l2 = get(0, 'level1'), get(1, 'level2')
        i1, i2 = get(2, 'levelIndex1'), get(3, 'levelIndex2')
        iname = next((lv.get('name') for lv in lvs if lv.get('name') == 'i'), 'i')
        vname = next((lv.get('name') for lv in lvs if lv.get('name') == 'val'), 'val')
        lines = []
        emit = lambda level, text: lines.append('  ' * level + text)
        emit(0, f"function LevelCmp({l1}, {l2})")
        emit(1, f"local {i1}, {i2}")
        emit(1, f"for {iname}, {vname} in ipairs(ArcLevels) do")
        emit(2, f"if {vname}.name == {l1} then"); emit(3, f"{i1} = {iname}"); emit(2, 'end')
        emit(2, f"if {vname}.name == {l2} then"); emit(3, f"{i2} = {iname}"); emit(2, 'end')
        emit(1, 'end')
        emit(1, f"if {i1} == nil then")
        emit(2, f"if {i2} == nil then"); emit(3, 'return -3')
        emit(2, 'else'); emit(3, 'return -1'); emit(2, 'end')
        emit(1, f"elseif {i2} == nil then"); emit(2, 'return -2')
        emit(1, f"elseif {i1} < {i2} then"); emit(2, 'return 1')
        emit(1, f"elseif {i1} > {i2} then"); emit(2, 'return 3')
        emit(1, f"elseif {i1} == {i2} then"); emit(2, 'return 2')
        emit(1, 'else'); emit(2, 'error("Unexpected")'); emit(1, 'end')
        emit(0, 'end')
        return lines


# ============================================================
# Lua 后处理器
# ============================================================

class LuaPostProcessor:
    def process(self, lines):
        cur = list(lines) if isinstance(lines, list) else str(lines).split('\n')
        cur = self._collapse_trivial_return_functions(cur)
        cur = self._merge_nested_and_conditions(cur)
        cur = self._simplify_wrapped_not_conditions(cur)
        cur = self._align_special_call_blocks(cur)
        cur = self._normalize_reference_constructors(cur)
        cur = self._normalize_function_declaration_spacing(cur)
        cur = self._normalize_common_call_spacing(cur)
        cur = self._normalize_call_expression_spacing(cur)
        cur = self._normalize_nil_comparisons(cur)
        cur = self._format_vars_style_root_table(cur)
        cur = self._format_vars_classic_layout(cur)
        cur = self._insert_top_level_block_spacing(cur)
        cur = self._compact_vars_root_spacing(cur)
        return cur

    def _collapse_trivial_return_functions(self, lines):
        out = []; i = 0
        while i < len(lines):
            line = lines[i]; nxt = lines[i + 1] if i + 1 < len(lines) else None
            third = lines[i + 2] if i + 2 < len(lines) else None
            if not line or nxt is None or third is None:
                out.append(line); i += 1; continue
            indent = self._leading_spaces(line)
            trimmed = line.strip(); next_t = nxt.strip(); third_t = third.strip()
            if (trimmed.startswith('function ') and trimmed.endswith(')')
                    and self._leading_spaces(nxt) == indent + 2
                    and next_t.startswith('return ')
                    and self._leading_spaces(third) == indent and third_t == 'end'):
                out.append(f"{line} {next_t} end"); i += 3; continue
            out.append(line); i += 1
        return out

    def _merge_nested_and_conditions(self, lines):
        cur = list(lines); changed = True
        while changed:
            changed = False; out = []; i = 0
            while i < len(cur):
                merged = self._try_merge_nested_if(cur, i)
                if not merged: out.append(cur[i]); i += 1; continue
                out.extend(merged['lines']); i = merged['nextIndex'] + 1; changed = True
            cur = out
        return cur

    def _try_merge_nested_if(self, lines, start):
        if start + 1 >= len(lines): return None
        m1 = re.match(r'^(\s*)if (.+) then$', lines[start])
        m2 = re.match(r'^(\s*)if (.+) then$', lines[start + 1])
        if not m1 or not m2: return None
        oi, ii = m1.group(1), m2.group(1)
        if len(ii) != len(oi) + 2: return None
        end = self._find_block_end(lines, start + 1, ii)
        if end < 0 or end + 1 >= len(lines): return None
        if lines[end + 1] != f"{oi}end": return None
        body = lines[start + 2:end]
        if not self._can_inline_nested_if_body(body, len(ii) + 2): return None
        return {'lines': [f"{oi}if {m1.group(2)} and {m2.group(2)} then",
                          *self._dedent_lines(body, 2), f"{oi}end"],
                'nextIndex': end + 1}

    def _find_block_end(self, lines, start, indent):
        for i in range(start + 1, len(lines)):
            if lines[i] == f"{indent}end": return i
        return -1

    def _can_inline_nested_if_body(self, lines, min_indent):
        if not lines: return False
        for line in lines:
            t = line.strip()
            if not t: continue
            if t == 'else' or t.startswith('elseif '): return False
            if self._leading_spaces(line) < min_indent: return False
        return True

    def _simplify_wrapped_not_conditions(self, lines):
        return [re.sub(r'^(\s*if not) \((.+)\) then$', r'\1 \2 then', line) for line in lines]

    def _align_special_call_blocks(self, lines):
        out = list(lines); i = 0
        while i < len(out):
            blk = self._read_special_call_block(out, i)
            if not blk: i += 1; continue
            formatted = self._format_special_call_block(blk['kind'], blk['lines'])
            for k in range(len(formatted)):
                out[blk['start'] + k] = formatted[k]
            i = blk['end'] + 1
        return out

    def _read_special_call_block(self, lines, start):
        if start >= len(lines): return None
        if not lines[start] or not lines[start].strip().endswith('{'): return None
        kind = None; blk = []; i = start + 1
        while i < len(lines):
            t = lines[i].strip()
            if t == '}': break
            if not t: return None
            if not kind:
                if t.startswith('level('): kind = 'level'
                elif t.startswith('bundle('): kind = 'bundle'
                else: return None
            if not t.startswith(f'{kind}(') or not t.endswith('),'): return None
            blk.append(lines[i]); i += 1
        if not kind or not blk or i >= len(lines) or lines[i].strip() != '}': return None
        return {'kind': kind, 'start': start + 1, 'end': i - 1, 'lines': blk}

    def _format_special_call_block(self, kind, lines):
        parsed = [self._parse_special_call_line(kind, line) for line in lines]
        if any(p is None for p in parsed): return lines
        col_count = max(len(p['args']) for p in parsed)
        widths = [0] * col_count
        for p in parsed:
            for i, a in enumerate(p['args']):
                widths[i] = max(widths[i], len(a))
        result = []
        for p in parsed:
            padded = [a if i == len(p['args']) - 1 else a.ljust(widths[i])
                      for i, a in enumerate(p['args'])]
            result.append(f"{p['indent']}{kind}( {', '.join(padded)} ),")
        return result

    def _parse_special_call_line(self, kind, line):
        m = re.match(r'^(\s*)[A-Za-z_][A-Za-z0-9_]*\((.*)\),$', line)
        if not m: return None
        args = self._split_top_level_args(m.group(2).strip())
        if not args: return None
        return {'indent': m.group(1), 'args': args, 'kind': kind}

    def _split_top_level_args(self, text):
        args = []; cur = ''; depth = 0; in_str = False; esc = False
        for ch in text:
            if in_str:
                cur += ch
                if esc: esc = False; continue
                if ch == '\\': esc = True; continue
                if ch == '"': in_str = False
                continue
            if ch == '"': in_str = True; cur += ch; continue
            if ch in '{[(': depth += 1; cur += ch; continue
            if ch in '}])': depth = max(0, depth - 1); cur += ch; continue
            if ch == ',' and depth == 0: args.append(cur.strip()); cur = ''; continue
            cur += ch
        if cur.strip(): args.append(cur.strip())
        return args

    def _dedent_lines(self, lines, spaces):
        out = []
        for line in lines:
            if not line.strip(): out.append(line); continue
            out.append(line[min(self._leading_spaces(line), spaces):])
        return out

    def _normalize_reference_constructors(self, lines):
        out = []; i = 0
        while i < len(lines):
            level = self._try_normalize_level_constructor(lines, i)
            if level: out.extend(level['lines']); i = level['nextIndex'] + 1; continue
            bundle = self._try_normalize_bundle_constructor(lines, i)
            if bundle: out.extend(bundle['lines']); i = bundle['nextIndex'] + 1; continue
            out.append(lines[i]); i += 1
        return out

    def _normalize_function_declaration_spacing(self, lines):
        out = []
        for line in lines:
            m = re.match(r'^(\s*function\s+[A-Za-z_][A-Za-z0-9_:.]*)\((.*)\)(.*)$', line)
            if not m: out.append(line); continue
            prefix, params, suffix = m.groups()
            params = params.strip()
            out.append(f"{prefix}(){suffix}" if not params else f"{prefix}( {params} ){suffix}")
        return out

    def _normalize_common_call_spacing(self, lines):
        out = []
        for line in lines:
            line = re.sub(r'\b(ipairs|pairs|table\.concat|table\.insert|print|error)\(([^()]+)\)',
                          lambda m: f"{m.group(1)}( {m.group(2).strip()} )", line)
            line = re.sub(r'([A-Za-z_][A-Za-z0-9_:.]*)\(([^()"]*)\)',
                          self._norm_spacing_repl, line)
            line = re.sub(r'\)\s+then\b', ') then', line)
            out.append(line)
        return out

    def _norm_spacing_repl(self, m):
        name, args = m.group(1), m.group(2)
        if ',' not in args and not args.strip(): return f"{name}()"
        if '"' in args or "'" in args: return m.group(0)
        return f"{name}( {args.strip()} )"

    def _normalize_call_expression_spacing(self, lines):
        return [self._norm_line_call_expr(line) for line in lines]

    def _norm_line_call_expr(self, line):
        patterns = [
            (re.compile(r'^(\s*)([A-Za-z_][A-Za-z0-9_:.]*)\((.*)\)$'), False),
            (re.compile(r'^(\s*return\s+)([A-Za-z_][A-Za-z0-9_:.]*)\((.*)\)$'), False),
            (re.compile(r'^(\s*if\s+not\s+)([A-Za-z_][A-Za-z0-9_:.]*)\((.*)\)(\s+then)$'), True),
        ]
        for pat, is_if in patterns:
            m = pat.match(line)
            if not m: continue
            if is_if:
                p, n, a, s = m.groups()
                return f"{p}{self._norm_call_text(n, a)}{s}"
            p, n, a = m.groups()
            return f"{p}{self._norm_call_text(n, a)}"
        return line

    def _norm_call_text(self, name, args_text):
        args = self._split_top_level_args(args_text)
        if not args: return f"{name}()"
        normalized = [self._norm_call_arg(a) for a in args]
        return f"{name}( {', '.join(normalized)} )"

    def _norm_call_arg(self, arg):
        t = str(arg).strip()
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_:.]*)\((.*)\)$', t)
        return self._norm_call_text(m.group(1), m.group(2)) if m else t

    def _normalize_nil_comparisons(self, lines):
        out = []
        for line in lines:
            line = re.sub(r'\s+~= nil\b', ' ~= nil', line)
            line = re.sub(r'\s+== nil\b', ' == nil', line)
            for prefix in (r'^(\s*if )', r'^(\s*elseif )'):
                line = re.sub(prefix + r'(.+?) == nil then$', r'\1\2 == nil then', line)
                line = re.sub(prefix + r'(.+?) ~= nil then$', r'\1\2 ~= nil then', line)
            out.append(line)
        return out

    def _format_vars_style_root_table(self, lines):
        if len(lines) < 3: return lines
        if not lines[0].startswith('Vars = {') or lines[-1] != '}': return lines
        body = []
        for i in range(1, len(lines) - 1):
            if not lines[i].strip(): continue
            body.append(lines[i][2:] if lines[i].startswith('  ') else lines[i])
        if not body: return ['Vars =', '{}']
        out = ['Vars =', '{' + body[0]]
        out.extend(body[1:]); out.append('}')
        return out

    def _format_vars_classic_layout(self, lines):
        if len(lines) < 3 or lines[0] != 'Vars =' or not lines[1].startswith('{'): return lines
        entries = self._parse_vars_entries(lines[1:-1])
        if not entries: return lines
        out = ['Vars = ']
        for i, e in enumerate(entries):
            out.extend(self._format_vars_entry(e, i == 0))
        out.append('}')
        return out

    def _parse_vars_entries(self, lines):
        entries = []; i = 0
        while i < len(lines):
            line = str(lines[i]).strip() if i < len(lines) else ''
            if not line: i += 1; continue
            m = re.match(r'^\{?([A-Za-z_][A-Za-z0-9_]*) = (.+)$', line)
            if not m: i += 1; continue
            key = m.group(1); value = m.group(2).strip().rstrip(',')
            if not value.startswith('{'):
                entries.append({'key': key, 'parts': [value]}); i += 1; continue
            if value != '{':
                inline = re.match(r'^\{(.*)\}$', value)
                if inline:
                    inner = inline.group(1).strip()
                    parts = self._split_top_level_args(inner) if inner else []
                    entries.append({'key': key, 'parts': parts}); i += 1; continue
            parts = []; i += 1
            while i < len(lines):
                inner = str(lines[i]).strip()
                if inner in ('}', '},'): break
                parts.append(inner.rstrip(',')); i += 1
            entries.append({'key': key, 'parts': parts}); i += 1
        return entries

    def _format_vars_entry(self, entry, is_first):
        out = [f"{'{\t' if is_first else ',\t'}{entry['key']} = "]
        parts = [p for p in entry.get('parts', []) if p]
        if not parts: out.append('\t{ }'); return out
        out.append(f"\t{{\t{parts[0]}")
        for i in range(1, len(parts)): out.append(f"\t,\t{parts[i]}")
        out.append('\t}')
        return out

    def _compact_vars_root_spacing(self, lines):
        if len(lines) < 3 or lines[0] != 'Vars =' or not lines[1].startswith('{'): return lines
        return [lines[0], lines[1]] + [l for l in lines[2:] if l != '']

    def _try_normalize_level_constructor(self, lines, start):
        block = lines[start:start + 8]
        expected = ['function level(name, bundle, realm)',
                    '  local obj = {', '    name = name,', '    bundle = bundle,',
                    '    realm = realm ~= nil and realm or bundle,',
                    '  }', '  return obj', 'end']
        if block != expected: return None
        return {'lines': ['function level( name, bundle, realm )', '  local obj = {',
                          '    ["name"] = name,', '    ["bundle"] = bundle,',
                          '    ["realm"] = realm ~= nil and realm or bundle,',
                          '  }', '  return obj', 'end'],
                'nextIndex': start + 7}

    def _try_normalize_bundle_constructor(self, lines, start):
        block = lines[start:start + 11]
        expected = ['function bundle(name, autoRequest, defaultPriority, next, text, prerequisites)',
                    '  local obj = {', '    name = name,', '    auto = autoRequest,',
                    '    prio = defaultPriority,', '    next = next,', '    text = text,',
                    '    prereq = prerequisites,', '  }', '  return obj', 'end']
        if block != expected: return None
        return {'lines': ['function bundle( name, autoRequest, defaultPriority, next, text, prerequisites )',
                          '  local obj = { ["name"] = name, ["auto"] = autoRequest, ["prio"] = defaultPriority, ["next"] = next, ["text"] = text, ["prereq"] = prerequisites }',
                          '  return obj', 'end'],
                'nextIndex': start + 10}

    def _insert_top_level_block_spacing(self, lines):
        out = []
        for line in lines:
            trimmed = line.strip(); prev = out[-1] if out else None
            if (trimmed and self._leading_spaces(line) == 0 and out and prev != ''
                    and not self._keep_adjacent(prev, trimmed)
                    and self._sep_top_level(trimmed)):
                out.append('')
            out.append(line)
        return out

    def _keep_adjacent(self, prev_line, current):
        prev = str(prev_line).strip()
        if not prev: return False
        one = r'^function\s+[A-Za-z_][A-Za-z0-9_:.]*\([^)]*\)\s+return\s+.+\s+end$'
        return bool(re.match(one, prev) and re.match(one, current))

    def _sep_top_level(self, trimmed):
        if trimmed.startswith('function ') or trimmed.startswith('if '): return True
        if re.match(r'^[A-Za-z_][A-Za-z0-9_]*\s*=\s*\{$', trimmed): return True
        return re.match(r'^[A-Za-z_][A-Za-z0-9_]*\s*=', trimmed) is not None

    def _leading_spaces(self, line):
        m = re.match(r'^ *', str(line))
        return len(m.group(0)) if m else 0


# ============================================================
# 服务层（单个 + 批量）
# ============================================================

class LuacDecompilerService:
    def __init__(self):
        self.parser = LuacParser()
        self.post_processor = LuaPostProcessor()

    def decompile(self, buffer):
        func = self.parser.parse(buffer)
        decompiler = FunctionDecompiler(func)
        lines = self.post_processor.process(decompiler.decompile())
        return {'func': func, 'lines': lines, 'content': '\n'.join(lines)}


def _resolve_luac(command=None):
    if command: return command
    local = Path(__file__).resolve().parent / 'tools' / 'lua-5.2.4' / 'src' / \
            ('luac.exe' if os.name == 'nt' else 'luac')
    return str(local) if local.exists() else 'luac'


class LuacCompiler:
    def __init__(self, command=None):
        self.command = command
        self.standard_parser = StandardLuacParser()
        self.client_writer = ClientLuacWriter()

    def get_command(self): return _resolve_luac(self.command)

    def compile(self, input_path, output_path):
        temp_dir = tempfile.mkdtemp(prefix='that-luac-')
        temp_out = os.path.join(temp_dir, 'intermediate.luac')
        command = self.get_command()
        try:
            out_dir = os.path.dirname(output_path)
            if out_dir and out_dir != '.': os.makedirs(out_dir, exist_ok=True)
            try:
                result = subprocess.run(
                    [command, '-o', temp_out, input_path],
                    capture_output=True, text=True)
            except FileNotFoundError:
                raise RuntimeError(
                    f'Compiler "{command}" not found. '
                    f'Please install Lua 5.2 luac or use --compiler <path>.')
            if result.returncode != 0:
                err = ((result.stderr or '') + '\n' + (result.stdout or '')).strip()
                raise RuntimeError(err or f'Compiler "{command}" exited with code {result.returncode}')
            with open(temp_out, 'rb') as f:
                standard_buffer = f.read()
            parsed = self.standard_parser.parse(standard_buffer)
            client_buffer = self.client_writer.write(parsed['func'])
            with open(output_path, 'wb') as f:
                f.write(client_buffer)
            return {'command': command, 'stdout': result.stdout or '', 'stderr': result.stderr or ''}
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class BatchConverter:
    """批量反编译 / 批量编译"""

    def __init__(self):
        self.decompiler_service = LuacDecompilerService()

    def batch_decompile(self, input_dir, output_dir=None, recursive=False,
                        pattern='*.luac', progress=None):
        input_path = Path(input_dir).resolve()
        if not input_path.is_dir():
            raise ValueError(f'不是目录: {input_dir}')
        output_path = Path(output_dir).resolve() if output_dir else input_path

        files = self._collect_files(input_path, pattern, recursive)
        stats = {'total': len(files), 'success': 0, 'failed': 0,
                 'errors': [], 'outputs': []}

        for idx, src in enumerate(files, 1):
            rel = src.relative_to(input_path)
            dst = output_path / rel.parent / self._decompile_out_name(rel.name)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                with open(src, 'rb') as f:
                    buf = f.read()
                result = self.decompiler_service.decompile(buf)
                with open(dst, 'w', encoding='utf-8') as f:
                    f.write(result['content'])
                stats['success'] += 1
                stats['outputs'].append(str(dst))
                if progress: progress(idx, len(files), str(src), True, None)
            except Exception as e:
                stats['failed'] += 1
                stats['errors'].append((str(src), str(e)))
                if progress: progress(idx, len(files), str(src), False, str(e))
        return stats

    def batch_compile(self, input_dir, output_dir=None, recursive=False,
                      pattern='*.lua', compiler_path=None, progress=None):
        input_path = Path(input_dir).resolve()
        if not input_path.is_dir():
            raise ValueError(f'不是目录: {input_dir}')
        output_path = Path(output_dir).resolve() if output_dir else input_path

        compiler = LuacCompiler(command=compiler_path)
        files = self._collect_files(input_path, pattern, recursive)
        stats = {'total': len(files), 'success': 0, 'failed': 0,
                 'errors': [], 'outputs': []}

        for idx, src in enumerate(files, 1):
            rel = src.relative_to(input_path)
            dst = output_path / rel.parent / self._compile_out_name(rel.name)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                compiler.compile(str(src), str(dst))
                stats['success'] += 1
                stats['outputs'].append(str(dst))
                if progress: progress(idx, len(files), str(src), True, None)
            except Exception as e:
                stats['failed'] += 1
                stats['errors'].append((str(src), str(e)))
                if progress: progress(idx, len(files), str(src), False, str(e))
        return stats

    def _collect_files(self, directory, pattern, recursive):
        globber = directory.rglob if recursive else directory.glob
        return sorted(p for p in globber(pattern) if p.is_file())

    def _decompile_out_name(self, name):
        p = Path(name)
        if not p.suffix: return name + '.lua'
        if p.suffix.lower() != '.luac': return p.stem + '.lua'
        stem = p.stem
        if stem.lower().endswith('.lua'): return stem
        return stem + '.lua'

    def _compile_out_name(self, name):
        p = Path(name)
        if not p.suffix: return name + '.luac'
        if p.suffix.lower() != '.lua': return p.stem + '.luac'
        return p.stem + '.luac'


# ============================================================
# 终端交互界面 (TUI)
# ============================================================

BANNER = r"""
 _                             _                       _
| |   _   _  __ _   ___       | |_   _  __ _  ___
| |  | | | |/ _` | / __|      | | | | |/ _` |/ __|
| |__| |_| | (_| || (__       | | |_| | (_| | (__
|_____\__,_|\__,_| \___|      |_|\__,_|\__,_|\___|

    Lua 5.2 luac Decompiler / Compiler  (Python edition)
"""


def _clear():
    if os.name == 'nt': os.system('cls')
    else: print('\033[2J\033[H', end='')


def _pause(msg='按回车键继续...'):
    try: input(msg)
    except EOFError: pass


def _prompt_path(prompt, default=None):
    suffix = f" [{default}]" if default else ''
    try: value = input(f"{prompt}{suffix}: ").strip()
    except EOFError: return ''
    if not value: return default or ''
    return value.strip('"').strip("'")


def _prompt_yes_no(prompt, default=False):
    suffix = " [Y/n]" if default else " [y/N]"
    try: value = input(f"{prompt}{suffix}: ").strip().lower()
    except EOFError: return default
    if not value: return default
    return value in ('y', 'yes', '是', '1', 'true')


def _default_decompile_output(path):
    p = Path(path)
    if not p.suffix: return str(p) + '.lua'
    if p.suffix.lower() != '.luac': return str(p.with_suffix('.lua'))
    base = p.stem
    if not base.lower().endswith('.lua'): base = base + '.lua'
    return str(p.parent / base)


def _default_compile_output(path):
    p = Path(path)
    if not p.suffix: return str(p) + '.luac'
    if p.suffix.lower() != '.lua': return str(p.with_suffix('.luac'))
    return str(p.parent / f"{p.stem}.luac")


def _tui_decompile():
    _clear(); print(BANNER)
    print(" === 反编译 .luac -> .lua ===\n")
    input_path = _prompt_path("输入 .luac 文件路径")
    if not input_path: return
    if not os.path.isfile(input_path):
        print(f"\n  ✗ 文件不存在: {input_path}"); _pause(); return
    output_path = _prompt_path("输出 .lua 文件路径", _default_decompile_output(input_path))
    try:
        with open(input_path, 'rb') as f: buffer = f.read()
        print(f"\n  读取: {input_path} ({len(buffer)} 字节)")
        result = LuacDecompilerService().decompile(buffer)
        out_dir = os.path.dirname(output_path)
        if out_dir and out_dir != '.': os.makedirs(out_dir, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f: f.write(result['content'])
        print(f"\n  ✓ 反编译成功")
        print(f"  ✓ 输出: {output_path}")
        print(f"  ✓ 共 {len(result['lines'])} 行")
    except Exception as e:
        print(f"\n  ✗ 反编译失败: {e}")
    _pause()


def _tui_compile():
    _clear(); print(BANNER)
    print(" === 编译 .lua -> .luac ===\n")
    input_path = _prompt_path("输入 .lua 文件路径")
    if not input_path: return
    if not os.path.isfile(input_path):
        print(f"\n  ✗ 文件不存在: {input_path}"); _pause(); return
    output_path = _prompt_path("输出 .luac 文件路径", _default_compile_output(input_path))
    compiler_path = _prompt_path("luac 编译器路径", _resolve_luac(None))
    try:
        print(f"\n  编译: {input_path}")
        print(f"  编译器: {compiler_path}")
        compiler = LuacCompiler(command=compiler_path)
        result = compiler.compile(input_path, output_path)
        print(f"\n  ✓ 编译成功")
        print(f"  ✓ 输出: {output_path}")
        if result.get('stderr', '').strip():
            print(f"\n  编译器输出:\n  {result['stderr'].strip()}")
    except Exception as e:
        print(f"\n  ✗ 编译失败: {e}")
    _pause()


def _progress_printer(idx, total, src, ok, err):
    status = '✓' if ok else '✗'
    print(f"  [{idx}/{total}] {status} {os.path.basename(src)}")
    if err: print(f"        错误: {err}")


def _tui_batch_decompile():
    _clear(); print(BANNER)
    print(" === 批量反编译 .luac -> .lua ===\n")
    input_dir = _prompt_path("输入目录")
    if not input_dir or not os.path.isdir(input_dir):
        print(f"\n  ✗ 目录不存在: {input_dir}"); _pause(); return
    recursive = _prompt_yes_no("递归子目录", default=False)
    output_dir = _prompt_path("输出目录 (留空=原位)", "")
    pattern = _prompt_path("文件匹配模式", "*.luac")
    print()
    if not _prompt_yes_no(
            f"将处理 {input_dir} 中匹配 {pattern} 的文件，是否继续", default=True):
        return
    print()
    try:
        converter = BatchConverter()
        stats = converter.batch_decompile(
            input_dir, output_dir or None, recursive, pattern, _progress_printer)
        print()
        print(f"  完成: 成功 {stats['success']} / 失败 {stats['failed']} / 总计 {stats['total']}")
        if stats['failed'] > 0:
            print(f"  ✗ 有 {stats['failed']} 个文件失败，详见上方日志")
    except Exception as e:
        print(f"\n  ✗ 批量转换失败: {e}")
    _pause()


def _tui_batch_compile():
    _clear(); print(BANNER)
    print(" === 批量编译 .lua -> .luac ===\n")
    input_dir = _prompt_path("输入目录")
    if not input_dir or not os.path.isdir(input_dir):
        print(f"\n  ✗ 目录不存在: {input_dir}"); _pause(); return
    recursive = _prompt_yes_no("递归子目录", default=False)
    output_dir = _prompt_path("输出目录 (留空=原位)", "")
    pattern = _prompt_path("文件匹配模式", "*.lua")
    compiler_path = _prompt_path("luac 编译器路径", _resolve_luac(None))
    print()
    if not _prompt_yes_no(
            f"将处理 {input_dir} 中匹配 {pattern} 的文件，是否继续", default=True):
        return
    print()
    try:
        converter = BatchConverter()
        stats = converter.batch_compile(
            input_dir, output_dir or None, recursive, pattern,
            compiler_path, _progress_printer)
        print()
        print(f"  完成: 成功 {stats['success']} / 失败 {stats['failed']} / 总计 {stats['total']}")
        if stats['failed'] > 0:
            print(f"  ✗ 有 {stats['failed']} 个文件失败，详见上方日志")
    except Exception as e:
        print(f"\n  ✗ 批量编译失败: {e}")
    _pause()


def _tui_help():
    _clear(); print(BANNER)
    print(" === 帮助 ===\n")
    print("  单个文件模式：")
    print("    1) 反编译 (decompile)  .luac -> .lua")
    print("    2) 编译 (compile)      .lua  -> .luac（需 Lua 5.2 luac）\n")
    print("  批量模式：")
    print("    3) 批量反编译  扫描目录内所有匹配 *.luac，输出 .lua")
    print("    4) 批量编译    扫描目录内所有匹配 *.lua，输出 .luac")
    print("    - 可选递归子目录")
    print("    - 可指定输出目录（不指定则原位生成）")
    print("    - 输出会保留原目录相对结构\n")
    print("  编译器查找顺序：")
    print("    1) 命令行 --compiler / -c 指定的路径")
    print(f"    2) 脚本目录下 tools/lua-5.2.4/src/luac")
    print("    3) 系统 PATH 中的 luac\n")
    print("  命令行用法：")
    print("    python lua_luac_tool.py                            # 启动界面")
    print("    python lua_luac_tool.py decompile <in> [out]")
    print("    python lua_luac_tool.py compile   <in> [out] [-c <path>]")
    print("    python lua_luac_tool.py batch-decompile <dir> [-o <out_dir>] [-r] [--pattern *.luac]")
    print("    python lua_luac_tool.py batch-compile   <dir> [-o <out_dir>] [-r] [-c <path>] [--pattern *.lua]")
    _pause()


def run_tui():
    while True:
        _clear(); print(BANNER)
        print("  1. 反编译 .luac → .lua")
        print("  2. 编译 .lua → .luac")
        print("  3. 批量反编译 (.luac → .lua)")
        print("  4. 批量编译   (.lua → .luac)")
        print("  5. 帮助")
        print("  0. 退出")
        print()
        try: choice = input("  请选择 [0-5]: ").strip()
        except EOFError: return 0
        if choice == '1': _tui_decompile()
        elif choice == '2': _tui_compile()
        elif choice == '3': _tui_batch_decompile()
        elif choice == '4': _tui_batch_compile()
        elif choice == '5': _tui_help()
        elif choice in ('0', 'q', 'quit', 'exit'):
            print("\n  再见！\n"); return 0


# ============================================================
# 命令行入口
# ============================================================

def _parse_named(tokens, key_aliases):
    """极简参数解析：返回 (positional, named)"""
    positional = []
    named = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        matched = None
        for alias in key_aliases:
            if tok == alias:
                matched = key_aliases[alias]; break
        if matched:
            if i + 1 >= len(tokens):
                raise ValueError(f"选项 {tok} 缺少值")
            named[matched] = tokens[i + 1]; i += 2; continue
        if tok == '-r' or tok == '--recursive':
            named['recursive'] = True; i += 1; continue
        if tok.startswith('-'):
            raise ValueError(f"未知选项: {tok}")
        positional.append(tok); i += 1
    return positional, named


def _run_single_decompile(input_path, output_path):
    if not input_path: raise ValueError("缺少输入文件")
    if not output_path: output_path = _default_decompile_output(input_path)
    with open(input_path, 'rb') as f: buffer = f.read()
    print(f"读取: {input_path} ({len(buffer)} 字节)")
    result = LuacDecompilerService().decompile(buffer)
    out_dir = os.path.dirname(output_path)
    if out_dir and out_dir != '.': os.makedirs(out_dir, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f: f.write(result['content'])
    print(f"输出: {output_path} ({len(result['lines'])} 行)")


def _run_single_compile(input_path, output_path, compiler_path):
    if not input_path: raise ValueError("缺少输入文件")
    if not output_path: output_path = _default_compile_output(input_path)
    compiler = LuacCompiler(command=compiler_path)
    print(f"编译: {input_path}")
    print(f"编译器: {compiler.get_command()}")
    compiler.compile(input_path, output_path)
    print(f"输出: {output_path}")


def _run_batch_decompile(input_dir, output_dir, recursive, pattern):
    converter = BatchConverter()
    stats = converter.batch_decompile(
        input_dir, output_dir or None, recursive, pattern, _progress_printer)
    print()
    print(f"完成: 成功 {stats['success']} / 失败 {stats['failed']} / 总计 {stats['total']}")
    return 0 if stats['failed'] == 0 else 1


def _run_batch_compile(input_dir, output_dir, recursive, pattern, compiler_path):
    converter = BatchConverter()
    stats = converter.batch_compile(
        input_dir, output_dir or None, recursive, pattern,
        compiler_path, _progress_printer)
    print()
    print(f"完成: 成功 {stats['success']} / 失败 {stats['failed']} / 总计 {stats['total']}")
    return 0 if stats['failed'] == 0 else 1


def run_cli(argv):
    if not argv: return run_tui()
    cmd = argv[0]
    if cmd in ('-h', '--help', 'help'):
        print(__doc__); return 0
    try:
        if cmd == 'decompile':
            pos, named = _parse_named(argv[1:], {'-i': 'input', '--input': 'input',
                                                 '-o': 'output', '--output': 'output'})
            input_path = named.get('input') or (pos[0] if pos else None)
            output_path = named.get('output') or (pos[1] if len(pos) > 1 else None)
            _run_single_decompile(input_path, output_path); return 0

        if cmd == 'compile':
            pos, named = _parse_named(argv[1:], {'-i': 'input', '--input': 'input',
                                                 '-o': 'output', '--output': 'output',
                                                 '-c': 'compiler', '--compiler': 'compiler'})
            input_path = named.get('input') or (pos[0] if pos else None)
            output_path = named.get('output') or (pos[1] if len(pos) > 1 else None)
            _run_single_compile(input_path, output_path, named.get('compiler'))
            return 0

        if cmd == 'batch-decompile':
            pos, named = _parse_named(argv[1:], {'-i': 'input', '--input': 'input',
                                                 '-o': 'output', '--output': 'output',
                                                 '--pattern': 'pattern'})
            input_dir = named.get('input') or (pos[0] if pos else None)
            if not input_dir: raise ValueError("缺少输入目录")
            return _run_batch_decompile(
                input_dir, named.get('output'),
                bool(named.get('recursive')), named.get('pattern', '*.luac'))

        if cmd == 'batch-compile':
            pos, named = _parse_named(argv[1:], {'-i': 'input', '--input': 'input',
                                                 '-o': 'output', '--output': 'output',
                                                 '-c': 'compiler', '--compiler': 'compiler',
                                                 '--pattern': 'pattern'})
            input_dir = named.get('input') or (pos[0] if pos else None)
            if not input_dir: raise ValueError("缺少输入目录")
            return _run_batch_compile(
                input_dir, named.get('output'),
                bool(named.get('recursive')), named.get('pattern', '*.lua'),
                named.get('compiler'))

        raise ValueError(f"未知命令: {cmd}")
    except Exception as e:
        print(f"错误: {e}")
        return 1


def main():
    try:
        sys.exit(run_cli(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\n已中断"); sys.exit(130)


if __name__ == '__main__':
    main()