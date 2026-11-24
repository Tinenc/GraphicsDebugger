using System;
using System.Collections.Generic;
using UnityEngine;

namespace Bread
{
    [Serializable]
    public sealed class BreadParameterBinding
    {
        public string name;
        public string type;
        public string sourceIdentity;
        public string scalarType;
        public float[] values = Array.Empty<float>();
        public int integer;
        public int components = 1;
        public int stride = 4;
        public string bufferBase64;
        public string bufferType = "structured";
        public long initialCounter = -1;
        public string counterProvenance;
    }

    // Material assets cannot serialize matrices, arrays or GPU buffer bindings.
    // Keep their authoritative data on the prefab and rebind after every reload.
    [ExecuteAlways, DisallowMultipleComponent]
    public sealed class BreadCaptureBindings : MonoBehaviour
    {
        public Renderer target;
        public bool parameterOnly;
        public BreadParameterBinding[] bindings = Array.Empty<BreadParameterBinding>();
        [NonSerialized] public string lastError;
        private MaterialPropertyBlock block;
        private readonly Dictionary<string, ComputeBuffer> buffers = new Dictionary<string, ComputeBuffer>();
        private readonly Dictionary<string, string> bufferData = new Dictionary<string, string>();

        private void OnEnable() { Apply(); }
        private void LateUpdate() { Apply(); }
        private void OnDisable() { if (target != null) target.SetPropertyBlock(null); Release(); }
        private void OnDestroy() { Release(); }

        public static Matrix4x4 Matrix(float[] values, int offset = 0)
        {
            if (values == null || values.Length < offset + 16) throw new ArgumentException("A matrix needs 16 row-major values.");
            var result = new Matrix4x4();
            for (int row = 0; row < 4; row++) for (int column = 0; column < 4; column++)
                result[row, column] = values[offset + row * 4 + column];
            return result;
        }

        public void Apply()
        {
            try
            {
                if (target == null) target = GetComponent<Renderer>();
                if (target == null && !parameterOnly) throw new InvalidOperationException("Capture bindings require a renderer or an effect parameter store.");
                if (block == null) block = new MaterialPropertyBlock();
                // This component owns the imported renderer's override block.
                // Rebuild it so removed bindings and shorter arrays do not leave
                // values from a previous repair attached to the renderer.
                block.Clear();
                var activeBuffers = new HashSet<string>();
                foreach (var binding in bindings)
                {
                    if (String.IsNullOrEmpty(binding.name)) throw new ArgumentException("A binding needs a property name.");
                    var values = binding.values ?? Array.Empty<float>();
                    switch (binding.type)
                    {
                        case "float": block.SetFloat(binding.name, values[0]); break;
                        case "int": block.SetInteger(binding.name, binding.integer); break;
                        case "vector": case "color":
                            block.SetVector(binding.name, new Vector4(values[0], values.Length > 1 ? values[1] : 0,
                                values.Length > 2 ? values[2] : 0, values.Length > 3 ? values[3] : 0)); break;
                        case "matrix": block.SetMatrix(binding.name, Matrix(values)); break;
                        case "float-array": block.SetFloatArray(binding.name, values); break;
                        case "vector-array":
                            if (binding.components < 1 || binding.components > 4 || values.Length % binding.components != 0)
                                throw new ArgumentException("Vector array dimensions disagree.");
                            var vectors = new Vector4[values.Length / binding.components];
                            for (int i = 0; i < vectors.Length; i++) for (int j = 0; j < binding.components; j++)
                                vectors[i][j] = values[i * binding.components + j];
                            block.SetVectorArray(binding.name, vectors); break;
                        case "matrix-array":
                            if (values.Length % 16 != 0) throw new ArgumentException("Matrix array dimensions disagree.");
                            var matrices = new Matrix4x4[values.Length / 16];
                            for (int i = 0; i < matrices.Length; i++) matrices[i] = Matrix(values, i * 16);
                            block.SetMatrixArray(binding.name, matrices); break;
                        case "buffer":
                            activeBuffers.Add(binding.name);
                            var encoded = binding.bufferBase64 ?? "";
                            if (!buffers.TryGetValue(binding.name, out var buffer) || !buffer.IsValid() ||
                                !bufferData.TryGetValue(binding.name, out var prior) || prior != encoded + ":" + binding.stride + ":" + binding.bufferType + ":" + binding.initialCounter)
                            {
                                buffer?.Release();
                                var bytes = Convert.FromBase64String(encoded);
                                if (binding.stride < 4 || binding.stride % 4 != 0 || binding.stride > 2048 || bytes.Length == 0 ||
                                    bytes.Length % binding.stride != 0 || bytes.Length > 64 * 1024 * 1024)
                                    throw new ArgumentException("Buffer stride/length is invalid.");
                                if (binding.bufferType != "structured" && binding.bufferType != "raw" && binding.bufferType != "append" && binding.bufferType != "counter" || binding.bufferType == "raw" && binding.stride != 4)
                                    throw new ArgumentException("Use structured/raw/append/counter storage with matching Shader access.");
                                bool counted = binding.bufferType == "append" || binding.bufferType == "counter";
                                if (counted && (binding.initialCounter < 0 || binding.initialCounter > bytes.Length / binding.stride || String.IsNullOrEmpty(binding.counterProvenance)))
                                    throw new ArgumentException("Append/counter buffers require an explicit bounded initialCounter and its evidence or reconstruction provenance.");
                                buffer = new ComputeBuffer(bytes.Length / binding.stride, binding.stride,
                                    binding.bufferType == "raw" ? ComputeBufferType.Raw : binding.bufferType == "append" ? ComputeBufferType.Append : binding.bufferType == "counter" ? ComputeBufferType.Counter : ComputeBufferType.Structured);
                                var words = new uint[bytes.Length / 4];
                                Buffer.BlockCopy(bytes, 0, words, 0, bytes.Length);
                                buffer.SetData(words);
                                if (counted) buffer.SetCounterValue((uint)binding.initialCounter);
                                buffers[binding.name] = buffer;
                                bufferData[binding.name] = encoded + ":" + binding.stride + ":" + binding.bufferType + ":" + binding.initialCounter;
                            }
                            block.SetBuffer(binding.name, buffer); break;
                        default: throw new ArgumentException("Unsupported runtime binding type: " + binding.type);
                    }
                }
                if (target != null) target.SetPropertyBlock(block);
                foreach (var name in new List<string>(buffers.Keys))
                    if (!activeBuffers.Contains(name)) { buffers[name].Release(); buffers.Remove(name); bufferData.Remove(name); }
                lastError = null;
            }
            catch (Exception error) { lastError = error.Message; }
        }

        public uint[] ReadBuffer(string name)
        {
            if (!buffers.TryGetValue(name, out var buffer) || !buffer.IsValid()) return null;
            var words = new uint[buffer.count * buffer.stride / 4];
            buffer.GetData(words);
            return words;
        }

        public MaterialPropertyBlock ParameterBlock => block;
        public uint ReadCounter(string name)
        {
            var buffer = GetBuffer(name);
            if (buffer == null) throw new ArgumentException("Counter buffer is unavailable.");
            using (var counter = new ComputeBuffer(1, 4, ComputeBufferType.Raw))
            { ComputeBuffer.CopyCount(buffer, counter, 0); var value = new uint[1]; counter.GetData(value); return value[0]; }
        }
        public ComputeBuffer GetBuffer(string name) => buffers.TryGetValue(name, out var value) ? value : null;
        public void ResetInputs() { Release(); Apply(); }

        private void Release()
        {
            foreach (var buffer in buffers.Values) buffer.Release();
            buffers.Clear(); bufferData.Clear();
        }
    }
}
