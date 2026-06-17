
export type ImageLayout =
  | 'text_only'
  | 'right_split'
  | 'left_split'
  | 'full_image'
  | 'text_with_inline_image';

export interface ManifestSegment {
  id: string;                          // e.g. "5-1"
  visual_text: string;
  show_image: boolean;
  audio_file: string;                  // relative to job dir: "audio/seg_5-1.mp3"
  audio_duration_seconds: number;
  visual_start_seconds: number;        // GLOBAL timeline position
  audio_start_seconds: number;         // GLOBAL timeline position
}

export interface ManifestSlide {
  slide_index: number;
  start_seconds: number;               // global timeline position
  duration_seconds: number;
  title: string;
  image_layout: ImageLayout;
  image_path: string | null;           // relative to job dir, may be empty/null
  segments: ManifestSegment[];
}

export interface ManifestVideo {
  total_duration_seconds: number;
  resolution: [number, number];
  framerate: number;
}

export interface Manifest {
  video: ManifestVideo;
  lecture_title: string;
  lecture_filename: string;
  slides: ManifestSlide[];
}

export interface LectureVideoProps {
  manifest: Manifest;
}