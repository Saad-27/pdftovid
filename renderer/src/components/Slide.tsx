
import React from 'react';
import { AbsoluteFill, Audio, Sequence, staticFile, useVideoConfig } from 'remotion';
import type { ManifestSlide, ManifestSegment, ImageLayout } from '../types';
import { Segment } from './Segment';

interface SlideProps {
  slide: ManifestSlide;
}

export const Slide: React.FC<SlideProps> = ({ slide }) => {
  const { fps } = useVideoConfig();

  const segmentsLocal = slide.segments.map((seg) => ({
    seg,
    visualFromFrame: Math.max(
      0,
      Math.round((seg.visual_start_seconds - slide.start_seconds) * fps),
    ),
    audioFromFrame: Math.max(
      0,
      Math.round((seg.audio_start_seconds - slide.start_seconds) * fps),
    ),
    audioDurFrames: Math.max(1, Math.round(seg.audio_duration_seconds * fps)),
  }));

  return (
    <AbsoluteFill style={{ fontFamily: 'Inter, system-ui, sans-serif' }}>

      {segmentsLocal.map(({ seg, audioFromFrame, audioDurFrames }) =>
        seg.audio_file ? (
          <Sequence
            key={`audio-${seg.id}`}
            from={audioFromFrame}
            durationInFrames={audioDurFrames}
            layout="none"
          >
            <Audio src={staticFile(seg.audio_file)} />
          </Sequence>
        ) : null,
      )}

      <LayoutFrame layout={slide.image_layout} title={slide.title} imagePath={slide.image_path}>

        {segmentsLocal.map(({ seg, visualFromFrame }, idx) => {

          return (
            <Sequence
              key={`vis-${seg.id}`}
              from={visualFromFrame}

              durationInFrames={10 * 60 * 30}
              layout="none"
            >
              <Segment
                segment={seg}
                isFirst={idx === 0}
                layout={slide.image_layout}
              />
            </Sequence>
          );
        })}
      </LayoutFrame>
    </AbsoluteFill>
  );
};

// -- Layout shells ---------------------------------------------------------

interface LayoutFrameProps {
  layout: ImageLayout;
  title: string;
  imagePath: string | null;
  children: React.ReactNode;
}

const TITLE_BAR_HEIGHT = 120;
const SIDE_PADDING = 80;

const LayoutFrame: React.FC<LayoutFrameProps> = ({ layout, title, imagePath, children }) => {

  const titleNode = title ? (
    <div
      style={{
        position: 'absolute',
        top: 0,
        left: 0,
        right: 0,
        height: TITLE_BAR_HEIGHT,
        display: 'flex',
        alignItems: 'center',
        paddingLeft: SIDE_PADDING,
        paddingRight: SIDE_PADDING,
        fontSize: 56,
        fontWeight: 700,
        color: '#1a1a1a',
        borderBottom: '4px solid #e0e0e0',
        backgroundColor: '#ffffff',
      }}
    >
      {title}
    </div>
  ) : null;

  const contentTop = title ? TITLE_BAR_HEIGHT : 0;


  const imageSrc = imagePath ? staticFile(imagePath) : null;

  switch (layout) {
    case 'right_split':
      return (
        <>
          {titleNode}
          <div
            style={{
              position: 'absolute',
              top: contentTop,
              left: 0,
              right: 0,
              bottom: 0,
              display: 'flex',
              flexDirection: 'row',
            }}
          >
            <div style={{ flex: 1, minWidth: 0, padding: SIDE_PADDING, position: 'relative' }}>
              {children}
            </div>
            <div
              style={{
                flex: 1,
                minWidth: 0,
                padding: SIDE_PADDING,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              {imageSrc && (
                <img
                  src={imageSrc}
                  alt=""
                  style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain' }}
                />
              )}
            </div>
          </div>
        </>
      );

    case 'left_split':
      return (
        <>
          {titleNode}
          <div
            style={{
              position: 'absolute',
              top: contentTop,
              left: 0,
              right: 0,
              bottom: 0,
              display: 'flex',
              flexDirection: 'row',
            }}
          >
            <div
              style={{
                flex: 1,
                minWidth: 0,
                padding: SIDE_PADDING,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              {imageSrc && (
                <img
                  src={imageSrc}
                  alt=""
                  style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain' }}
                />
              )}
            </div>
            <div style={{ flex: 1, minWidth: 0, padding: SIDE_PADDING, position: 'relative' }}>
              {children}
            </div>
          </div>
        </>
      );

    case 'full_image':
      return (
        <>
          {titleNode}
          <div
            style={{
              position: 'absolute',
              top: contentTop,
              left: 0,
              right: 0,
              bottom: 0,
            }}
          >
            {imageSrc && (
              <img
                src={imageSrc}
                alt=""
                style={{
                  width: '100%',
                  height: '100%',
                  objectFit: 'contain',
                }}
              />
            )}
            {/* Text floats over the bottom 30% with a translucent backing. */}
            <div
              style={{
                position: 'absolute',
                left: 0,
                right: 0,
                bottom: 0,
                padding: SIDE_PADDING,
                backgroundColor: 'rgba(255,255,255,0.92)',
                minHeight: 200,
              }}
            >
              {children}
            </div>
          </div>
        </>
      );

    case 'text_with_inline_image':
      return (
        <>
          {titleNode}
          <div
            style={{
              position: 'absolute',
              top: contentTop,
              left: 0,
              right: 0,
              bottom: 0,
              display: 'flex',
              flexDirection: 'column',
              padding: SIDE_PADDING,
            }}
          >
            <div style={{ flex: 1, position: 'relative' }}>{children}</div>
            {imageSrc && (
              <div
                style={{
                  flex: 1,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  paddingTop: 40,
                }}
              >
                <img
                  src={imageSrc}
                  alt=""
                  style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain' }}
                />
              </div>
            )}
          </div>
        </>
      );

    case 'text_only':
    default:
      return (
        <>
          {titleNode}
          <div
            style={{
              position: 'absolute',
              top: contentTop,
              left: 0,
              right: 0,
              bottom: 0,
              padding: SIDE_PADDING,
            }}
          >
            {children}
          </div>
        </>
      );
  }
};