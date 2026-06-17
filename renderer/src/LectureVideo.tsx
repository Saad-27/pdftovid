
import React from 'react';
import { AbsoluteFill, Sequence, useVideoConfig } from 'remotion';
import { Slide } from './components/Slide';
import type { LectureVideoProps } from './types';

export const LectureVideo: React.FC<LectureVideoProps> = ({ manifest }) => {
  const { fps } = useVideoConfig();

  return (
    <AbsoluteFill style={{ backgroundColor: '#fafafa' }}>
      {manifest.slides.map((slide) => {
        const from = Math.round(slide.start_seconds * fps);
        const dur = Math.max(1, Math.round(slide.duration_seconds * fps));
        return (
          <Sequence
            key={slide.slide_index}
            from={from}
            durationInFrames={dur}

            name={`Slide ${slide.slide_index}`}
          >
            <Slide slide={slide} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};