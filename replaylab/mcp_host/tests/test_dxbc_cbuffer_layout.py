"""Stripped DXBC must retain physical declarations without invented members."""
import copy
import importlib.util
from pathlib import Path
import struct
from types import SimpleNamespace as NS

from tests.test_cbuffer_bridge import _load_bridge, _constant_type, _constant
from src.shader_workspace.abi import parse_shader_abi, compare_shader_abi
from src.shader_workspace.mutation_jobs import bridge_operation_id
from src.symbols.scanner import interface_is_complete

bridge = _load_bridge()
layout = bridge._dxbc_cbuffer_layout


def binary(stage=0, vectors=3, sm51=False, rdef=False, structured=False):
    operand = (8 << 12) | ((3 if sm51 else 2) << 20) | 2
    words = [(stage << 16) | (0x51 if sm51 else 0x50), 0]
    words += ([(7 << 24) | 89, operand, 0, 2, 2, vectors, 4] if sm51 else
              [(4 << 24) | 89, operand, 0, vectors])
    if structured: words += [(4 << 24) | 162, (7 << 12) | (1 << 20), 2, 16]
    words[1] = len(words)
    chunks = [(b'SHEX', struct.pack('<%dI' % len(words), *words))]
    if rdef: chunks.append((b'RDEF', b'present'))
    start = 32 + 4 * len(chunks)
    data, offsets = b'', []
    for tag, payload in chunks:
        offsets.append(start + len(data)); data += struct.pack('<4sI', tag, len(payload)) + payload
    return b'DXBC' + b'\0' * 16 + struct.pack('<III', 1, start + len(data), len(chunks)) + struct.pack('<%dI' % len(offsets), *offsets) + data


def reflection(stage='Fragment', vectors=3):
    t = _constant_type('VarType.Float', columns=4, elements=0, array_stride=16, matrix_stride=16)
    t.name = 'float4'
    return NS(stage=stage, entryPoint='main', encoding='DXBC', rawBytes=binary(vectors=vectors),
        constantBlocks=[NS(name='cbuffer0', byteSize=vectors*16, fixedBindNumber=0,
            fixedBindSetOrSpace=0, bindArraySize=1, bufferBacked=True,
            variables=[_constant('cb0_v%d'%i, i*16, copy.deepcopy(t)) for i in range(vectors)])])


def test_all_dxbc_stage_declarations_and_sm51_space():
    for stage in range(6):
        assert layout.declarations(binary(stage))[(0,0)]['byteSize'] == 48
    assert layout.declarations(binary(sm51=True))[(4,2)]['bindArraySize'] == 1
    assert not layout.declarations(binary(rdef=True))
    assert not layout.declarations(binary()[:-1])
    bad = bytearray(binary()); struct.pack_into('<I', bad, 48, 0)
    assert not layout.declarations(bytes(bad))


def test_stripped_fallback_is_opaque_and_typed_candidate_still_checked():
    original = reflection()
    iface = bridge._compiled_shader_interface(original)
    assert iface['interfaceComplete']
    assert iface['constantBlocks'][0]['layoutCompleteness'] == 'opaque'
    assert iface['constantBlocks'][0]['fields'] == []
    baseline = parse_shader_abi(iface)
    candidate = reflection()
    candidate.rawBytes = binary(rdef=True)
    for f in candidate.constantBlocks[0].variables: f.type.elements = 1
    assert compare_shader_abi(baseline, parse_shader_abi(bridge._compiled_shader_interface(candidate))).compatible
    candidate.constantBlocks[0].byteSize += 16
    assert not compare_shader_abi(baseline, parse_shader_abi(bridge._compiled_shader_interface(candidate))).compatible
    assert not bridge._physical_shader_interfaces_compatible(original, candidate)


def test_missing_or_contradictory_proof_never_downgrades_a_layout():
    for alter in (lambda r: setattr(r, 'rawBytes', b''),
                  lambda r: setattr(r.constantBlocks[0], 'byteSize', 64),
                  lambda r: setattr(r.constantBlocks[0].variables[0], 'name', 'ActualTypedField')):
        r = reflection(); alter(r)
        assert not bridge._compiled_shader_interface(r)['interfaceComplete']


def test_native_operation_accepts_stable_uuid_and_labels_without_repeating_write():
    assert bridge_operation_id('AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE') == 'aaaaaaaabbbbccccddddeeeeeeeeeeee'
    assert bridge_operation_id('apply-eid7195-final') == bridge_operation_id('apply-eid7195-final')
    assert bridge_operation_id('apply-eid7195-final') != bridge_operation_id('apply-eid7195-second')
    assert len(bridge_operation_id()) == 32
    assert bridge_operation_id('替换') == bridge_operation_id('替换')
    assert len(bridge_operation_id('apply')) == 32


def test_stripped_structured_srv_uses_declared_stride_and_rejects_changed_stride():
    original = reflection(); original.rawBytes = binary(structured=True)
    original.readOnlyResources = [NS(name='structuredbuffer2', fixedBindSetOrSpace=0,
        fixedBindNumber=2, bindArraySize=1, descriptorType='DescriptorType.Buffer',
        textureType='TextureType.Buffer', isTexture=False, isReadOnly=True,
        variableType=_constant_type('VarType.Unknown', rows=0, columns=0, elements=0))]
    iface = bridge._compiled_shader_interface(original)
    raw_type = iface['readOnlyResources'][0]['variableType']
    assert raw_type['layoutCompleteness'] == 'opaque' and raw_type['byteSize'] == 16
    assert interface_is_complete(iface)
    baseline = parse_shader_abi(iface)
    candidate = copy.deepcopy(iface)
    typed = candidate['readOnlyResources'][0]['variableType']
    typed.pop('layoutCompleteness'); typed.update(baseType='VarType.Float',rows=1,columns=4)
    assert compare_shader_abi(baseline, parse_shader_abi(candidate)).compatible
    assert not bridge._interface_incompatibilities(bridge._physical_shader_interface(iface),bridge._physical_shader_interface(candidate))
    typed.update(arrayByteStride=32,byteSize=32)
    assert not compare_shader_abi(baseline, parse_shader_abi(candidate)).compatible
    assert bridge._interface_incompatibilities(bridge._physical_shader_interface(iface),bridge._physical_shader_interface(candidate))
