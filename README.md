# PDF 2 narrated lecture video

I wanted to build a genuinely end-to-end system, so I picked a problem big enough to force the interesting parts: 

-a multi-stage pipeline

-two LLM passes feeding each other

-a real job queue 

-a worker decoupled from the API

-a security model for handling someone else's paid API key

and then taking it to production and making a deliberate cost-versus-performance call once it turned out CPU-bound.
If a read-aloud button does what you need, use the read-aloud button.

Feed it a PDF of lecture slides, get back a narrated MP4. Claude reads the deck, writes a script slide by slide, a neural TTS voice reads it out loud, and Remotion renders it into a video that reveals each point as it's spoken.


**See it run:**

 
- The original slides it ate: https://drive.google.com/file/d/1G7gh6ffnfpLxwaTZcMDoO4YYFmZkjsR1/view?usp=sharing
- Output video: https://youtu.be/50uRUjjVN_w

It used to be live. I took it down on purpose. **See below.**

## Why it's not live

- It shipped and worked across separate API and worker boxes with Neon, R2 and Redis behind it.
- Then I profiled it. It's CPU-bound and slow. Single-threaded TTS plus a Chromium render choking on shared cores, worker OOMing against a 4 GB ceiling.
- Fixing it properly means dedicated CPU at roughly 60 euro/month. A GPU if I wanted it genuinely fast.
- I wasn't paying that 
- So the hosted version is off, the full deploy config is still in the repo, and it runs locally on real cores when I want to demo it.



## How it works
 
Seven stages with a Postgres job queue in the middle. A job comes in, a worker claims it, and it walks the stages in order:
 
1. **Validate** the PDF, in the API itself. Size, page count, is it actually a PDF, is it encrypted. Cheap checks up front so you get an instant no instead of waiting for a worker to reject you.
2. **Extract** text and images with PyMuPDF. More heuristics here than you'd think. It skips the giant page number a deck renders in the corner when it's picking a slide title, buckets bullet indent levels relative to each slide's own left margin so wildly different templates don't all collapse to one level, and throws out background images that are really just decoration behind the text.
3. **Plan the whole deck.** One Claude pass reads every slide, the text, plus a thumbnail of any image, and produces a plan: a title, a section breakdown, and per-slide flags like "this continues the previous slide" and "this image is a real diagram, not decoration." It runs first so the next stage isn't scripting each slide blind.
4. **Script each slide.** A Claude call per slide writes the narration and splits it into reveal-as-you-go segments. Slides that continue each other get chained and scripted in sequence so each one can refer back, and independent chains run concurrently. Stages 3 and 4 together cost about 25 cents on a 50-slide deck.
5. **Synthesise speech** with Kokoro, a local neural TTS model. It runs on CPU, one segment at a time. Torch inference isn't thread-safe and a second model instance is another ~500 MB of RAM, so serial it is. There's a warm-up pass so the progress bar doesn't stall on segment one, and a per-language voice cache for the US and UK voices.
6. **Build a timeline manifest.** Pure deterministic logic, no network. Works out when each bullet appears, when its audio starts, and how long every slide runs. One JSON file the renderer treats as gospel.
7. **Render** with Remotion (React, but for video). It composites the layouts, fades bullets in, lines audio up to visuals, then ffmpeg muxes the final MP4.

   
