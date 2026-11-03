# Model-load error guidance screenshots

Playwright component previews using simulated runtime failures, not actual model loads.

Base: 867c4248d0aeea136669a079897d30b472b2afdf
PR head: 25b101df3d1cc5913faf38a5613582ecfb0073e9

The actual baseline and modified startup classifiers generated the messages. Both use the current Unsloth toast component; AFTER uses the PR's toast formatter. Log selection and full-error clipboard copying were asserted with Playwright. No user model files or real credentials were used.

These images are evidence only and are not included in the code PR's diff.
