# `mempalace.onnx_graph`

Source: [`mempalace/onnx_graph.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/onnx_graph.py)

Load-time rewrites of ONNX graphs, done on the protobuf wire format.

Kept dependency-free on purpose: the rewrite below touches a handful of
fields, so it reads and writes them with a small varint codec instead of
pulling in the ``onnx`` package. Every field it does not rewrite is copied
through byte for byte, so the result differs from the input only where it
means to.

The rewrite (#2681): a q8 model that stores its token table quantized looks
tokens up as ``Gather(DequantizeLinear(table_q, scale, zero_point), ids)``.
ONNX Runtime evaluates that literally, so every ``run()``, even for a
two-word query, first dequantizes the whole table into a float32 copy
(768 MiB for EmbeddingGemma's 262,144 x 768 table), and the CPU arena keeps
that high-water mark. With a per-tensor scale and zero point, dequantizing
is elementwise, so it commutes with the row lookup:
``DequantizeLinear(Gather(table_q, ids), scale, zero_point)`` produces the
same floats, bit for bit, while touching only the looked-up rows.

## Classes

### `class GatherPatch`

A rewritten model, ready for ``ort.InferenceSession(model_bytes, ...)``.

``external_data_dir`` is the real directory holding the model's external
data, for the ``session.model_external_initializers_file_folder_path``
session option; ``None`` when the model has no external data.

## Functions

### `gather_before_dequantize`

```python
def gather_before_dequantize(model_path: str) -> Optional[GatherPatch]
```

Swap every eligible ``Gather(DequantizeLinear(...))`` into ``DequantizeLinear(Gather(...))``.

A pair is eligible when the dequantized tensor is an initializer with a
per-tensor (scalar) scale and zero point, no block quantization, and
the Gather that reads it as its data input is its only consumer.
Returns ``None`` when no pair is eligible, so the caller loads the
model from its path as before, or when the external data cannot be
pointed at from memory (files spread over several real directories).

The model is returned as bytes because it is loaded from memory: its
external-data locations are rewritten to the files' real names in
their real directory, which also keeps a symlinked cache (huggingface_hub
links snapshot files to content-addressed blobs) inside the directory
ONNX Runtime validates external data against.
