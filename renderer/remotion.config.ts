/**
 * Remotion configuration. Only fields we explicitly want to pin.
 * Everything else uses Remotion defaults.
 *
 * H.264 + AAC matches PRD §4.7's "1080p H.264 + AAC" output contract.
 */
import { Config } from '@remotion/cli/config';

Config.setVideoImageFormat('jpeg');
Config.setCodec('h264');
// Default CRF is 18 (visually lossless-ish). 23 is the x264 default and gives
// us much smaller files for what is essentially text-on-solid-background video.
// PRD §7.3 calls out ~25 MB target video size, and CRF 23 lands us there for
// a 5-8 minute lecture render.
Config.setCrf(23);
// Concurrency = false lets Remotion pick based on CPU count. For a Fly.io
// worker we'll override this from the CLI (--concurrency=N) since the VM has
// fewer cores than a dev laptop.
Config.setConcurrency(null);