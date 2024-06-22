# Update log
#### v0.1.0 (2024-06-16)
**`[New]:`**
- Apply vector quantized common (VQC)-latent space for models (`VQ-Seq2Seq`).
- Add task-specific fusion (TSF) for models (`TSF-Seq2Seq`).
- Add new training strategy suitable for missing data.
- Sequence with `'anchor'` in `channel_names` will be set as anchor domain, add new anchor domain (Canny edge detection) and corresponding normalization scheme.
- Add discriminator and adversarial learning.
- Add segmentor, ignore to calculate segmentation loss for the subject with `'foreground'` in `labels`.
- New visualization during training.

**`[Update]:`**
- Update Seq2Seq models with self-attention and cross-attention.
- Update GPU memory limited to 12G.
- Update minimal batch size to 1.
- Update documents.

**`[Fix]:`**
- Remove data split in ``nnseq2seq/dataset_conversion/Dataset001_BraTS.py``.
- Fix bug of mismatch between normalization scheme and sequence name.

**`[Todo]:`**
- [ ] Improve perceptual loss for 3D model.
- [ ] Pre-trained models for multi-sequence breast MRI.

#### v0.0.1 (2024-03-26)
**`[New]:`**
- Initial release.