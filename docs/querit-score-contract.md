# Querit rerank score contract

The deployed weights are `Querit/Querit-4B`, revision
`7b796de30ad8dc772d6c46c75659c1341283a665`. Qwen reranker names are legacy
routing aliases only. They do not identify the weights or imply Qwen calibration.

## Input and probability

The publisher's [MTEB wrapper at commit 276fbe6](https://github.com/embeddings-benchmark/mteb/blob/276fbe60384c2f9ebd7f7de5009af8e33700cf2d/mteb/models/model_implementations/querit_models.py)
formats each pair as:

```text
Judge whether the Content meets the requirements based on the Query. Query: {query}; Content: {document}[CLS]
```

The wrapper tokenizes without additional special tokens, then appends token
151665 (`[CLS]`). Its content attention is causal; the final CLS sees all content.
The converted artifact therefore uses native causal sequence classification.
The source tokenizer appends token 151643 when `add_special_tokens=True`.
vLLM scoring forces that option, so conversion removes the tokenizer postprocessor.
Otherwise EOS follows CLS and native last-token pooling reads the wrong position.
The converted tokenizer's complete digest is pinned by the artifact verifier.

The [publisher head](https://huggingface.co/Querit/Querit-4B/blob/7b796de30ad8dc772d6c46c75659c1341283a665/modeling_querit_4b.py)
returns `s = p1 - p0`, where `[p0,p1] = softmax([z0,z1])`.
That signed domain is valid for the publisher, but not for consumers expecting
nonnegative probabilities. The native converted head uses:

```text
weight = head.weight[1] - head.weight[0]
bias = head.bias[1] - head.bias[0]
p1 = sigmoid(z1-z0) = (s+1)/2
```

Thus native `/v1/rerank` and `/v1/score` return finite classifier probabilities
in `[0,1]`. Guard's DeepInfra compatibility endpoint must preserve these values,
not map them again. This is exact head algebra, subject to BF16 rounding, not
empirical relevance calibration. Do not impose Qwen thresholds or normalize by
batch extrema, rank, or candidate count. A single candidate must retain its score.

## Reference and rollout acceptance

The pinned publisher implementation, official MTEB input methods, and original
BF16 weights were executed on CPU. Five real query/document pairs gave original
softmax p1 values `0.60546875`, `0.3515625`, `0.29296875`, `0.53515625`, and
`0.27734375`. The converted head matched each at the same real hidden state
with zero observed absolute error. These values are examples, not thresholds.

Run the converter and readiness unit tests, then `just pre-push`. Live acceptance
must compare the converted native results with the official reference within
0.01, verify relevant-over-unrelated ordering in English and Chinese, and compare
a fixed pair alone versus in ten candidates within 0.01. Check both canonical
and legacy names through authenticated Guard and Bifrost routes. Preserve missing
and invalid key rejection, overreach rejection, and unrelated service generations.

Deploy the native probability artifact and the compatible Guard adapter together,
with verified prior binaries, model artifact, configuration, and database recovery
prerequisites. Never overwrite the publisher's cache-owned snapshot. No replacement
model, second production model load, quantization change, or new inference engine
is required.
