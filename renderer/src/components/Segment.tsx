
import React from 'react';
import { interpolate, useCurrentFrame, useVideoConfig } from 'remotion';
import type { ManifestSegment, ImageLayout } from '../types';

interface SegmentProps {
  segment: ManifestSegment;
  isFirst: boolean;
  layout: ImageLayout;
}

const FADE_IN_FRAMES = 8;  // ~250ms at 30fps

export const Segment: React.FC<SegmentProps> = ({ segment, isFirst, layout }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const opacity = interpolate(frame, [0, FADE_IN_FRAMES], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  const translateY = interpolate(frame, [0, FADE_IN_FRAMES], [12, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  const isSplit =
    layout === 'right_split' ||
    layout === 'left_split' ||
    layout === 'text_with_inline_image';
  const baseFontSize = isSplit ? 36 : 44;
  const fontSize = isFirst ? baseFontSize + 6 : baseFontSize;
  const fontWeight = isFirst ? 600 : 400;

  return (
    <div
      style={{
        opacity,
        transform: `translateY(${translateY}px)`,
        fontSize,
        fontWeight,
        lineHeight: 1.35,
        color: '#1f2937',
        marginBottom: 28,
        maxWidth: '100%',
        ...(isFirst
          ? {}
          : {
              paddingLeft: 24,
              borderLeft: '4px solid #c7d2fe',
            }),
      }}
    >
      {segment.visual_text}
    </div>
  );
};