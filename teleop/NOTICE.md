# Upstream references and notices

This implementation was designed after reviewing the following upstream projects. Their code is not treated as an LM3-UP production safety layer.

- Seeed Studio Wiki (`Seeed-Studio/wiki-documents`), pinned as a Git submodule. Refer to the upstream repository for file-specific licensing notices.
- Phosphobot, commit `a5de051197c879e3c685d1362f649ce54ee47a3c`, MIT License. Its browser-relative-motion interaction informed the control UX review.
- Hugging Face LeRobot v0.4.2, commit `58f70b6bd370864139a3795ac3497a9eae8c42d5`, Apache-2.0 License. Its `examples/phone_to_so100` pipeline informed the phone-pose and dataset review.
- SpesRobotics `teleop`, commit `c5d808155a87b584d6147a5943d4b87c34c92db0`. Refer to that repository for its license and WebXR implementation notices.
- LingBot-VLA, commit `4eb34b7693a0565c67433f8fac9c59a2e67eb60b`, Apache-2.0 source repository. Model-weight and base-model licensing must still be verified before product use.

The LM3-UP mobile clients and safety bridge in this repository are purpose-built around the local robot manuals and the pinned Lebai SDK. No upstream safety claim is inherited.
