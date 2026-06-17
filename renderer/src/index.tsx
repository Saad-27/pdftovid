
import { registerRoot, Composition } from 'remotion';
import { LectureVideo } from './LectureVideo';
import type { Manifest, LectureVideoProps } from './types';

const STUDIO_PLACEHOLDER: Manifest = {
  video: {
    total_duration_seconds: 6,
    resolution: [1920, 1080],
    framerate: 30,
  },
  lecture_title: 'Studio Preview',
  lecture_filename: 'preview.pdf',
  slides: [
    {
      slide_index: 1,
      start_seconds: 0,
      duration_seconds: 6,
      title: 'Lecture Video Preview',
      image_layout: 'text_only',
      image_path: null,
      segments: [
        {
          id: '1-1',
          visual_text: 'No manifest loaded — pass --props to preview a real job.',
          show_image: false,
          audio_file: '',
          audio_duration_seconds: 2.5,
          visual_start_seconds: 0.8,
          audio_start_seconds: 1.3,
        },
      ],
    },
  ],
};

const RemotionRoot: React.FC = () => {
  return (

    <Composition
      id="LectureVideo"
      component={LectureVideo as unknown as React.ComponentType<Record<string, unknown>>}

      durationInFrames={Math.ceil(STUDIO_PLACEHOLDER.video.total_duration_seconds * STUDIO_PLACEHOLDER.video.framerate)}
      fps={STUDIO_PLACEHOLDER.video.framerate}
      width={STUDIO_PLACEHOLDER.video.resolution[0]}
      height={STUDIO_PLACEHOLDER.video.resolution[1]}
      defaultProps={{ manifest: STUDIO_PLACEHOLDER } as unknown as Record<string, unknown>}
      calculateMetadata={({ props }) => {
        const m = (props as unknown as LectureVideoProps).manifest;
        return {
          durationInFrames: Math.ceil(m.video.total_duration_seconds * m.video.framerate),
          fps: m.video.framerate,
          width: m.video.resolution[0],
          height: m.video.resolution[1],
        };
      }}
    />
  );
};

registerRoot(RemotionRoot);