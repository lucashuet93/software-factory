/**
 * Browser side of a video call with the lead engineer.
 *
 * Microphone audio is captured with an AudioWorklet, converted to PCM16 mono at 24 kHz, and sent to the local server,
 * which relays it to Azure Voice Live. The avatar's video and voice arrive directly from Azure over WebRTC; the server
 * only relays the offer and answer.
 */

export type CallState = "idle" | "connecting" | "ready" | "listening" | "thinking" | "speaking";
export type ToolState = "running" | "done" | "error";

export type CallHandlers = {
  onState: (state: CallState) => void;
  onCaption: (role: "user" | "assistant", text: string, final: boolean) => void;
  onTool: (name: string, state: ToolState) => void;
  onError: (message: string) => void;
  onMedia: (video: MediaStream | null, audio: MediaStream | null) => void;
  onEnded: (reason: string) => void;
};

type IceServer = { urls: string[] | string; username?: string; credential?: string };

const TARGET_RATE = 24000;
const CHUNK_SAMPLES = 1200; // 50 ms at 24 kHz

// Resamples whatever rate the device runs at to 24 kHz with linear interpolation; speech needs nothing fancier.
const WORKLET = `
class Pcm16Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / ${TARGET_RATE};
    this.pos = 0;
    this.buf = new Int16Array(${CHUNK_SAMPLES});
    this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    let pos = this.pos;
    while (pos < ch.length) {
      const i = Math.floor(pos);
      const a = ch[i];
      const b = i + 1 < ch.length ? ch[i + 1] : a;
      const s = Math.max(-1, Math.min(1, a + (b - a) * (pos - i)));
      this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.n === this.buf.length) {
        this.port.postMessage(this.buf.buffer, [this.buf.buffer]);
        this.buf = new Int16Array(${CHUNK_SAMPLES});
        this.n = 0;
      }
      pos += this.step;
    }
    this.pos = pos - ch.length;
    return true;
  }
}
registerProcessor("pcm16-capture", Pcm16Capture);
`;

export class CallClient {
  private ws: WebSocket | null = null;
  private pc: RTCPeerConnection | null = null;
  private mic: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private video: MediaStream | null = null;
  private audio: MediaStream | null = null;
  private muted = false;
  private ended = false;
  private readySent = false;

  constructor(private readonly projectId: string, private readonly on: CallHandlers) {}

  async start(): Promise<void> {
    this.on.onState("connecting");
    try {
      this.mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch {
      this.finish("Microphone access was blocked. Allow the microphone for this page and try again.", true);
      return;
    }
    if (this.ended) return;
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${window.location.host}/api/projects/${this.projectId}/call`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      this.startMicrophone().catch(() => this.finish("Could not start the microphone.", true));
    };
    ws.onmessage = (e) => {
      if (typeof e.data === "string") this.handle(JSON.parse(e.data));
    };
    ws.onclose = () => this.finish("Call ended");
  }

  /** The UI calls this once the avatar video is actually playing, so the greeting isn't spoken into the void. */
  avatarReady(): void {
    if (this.readySent) return;
    this.readySent = true;
    this.send({ type: "avatar.ready" });
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    this.mic?.getAudioTracks().forEach((t) => (t.enabled = !muted));
  }

  sendText(text: string): void {
    this.send({ type: "text", text });
  }

  hangup(): void {
    this.send({ type: "hangup" });
    this.finish("Call ended");
  }

  private send(msg: object): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  private async startMicrophone(): Promise<void> {
    const ctx = new AudioContext();
    this.ctx = ctx;
    const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
    try {
      await ctx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    if (this.ended || !this.mic) return;
    const source = ctx.createMediaStreamSource(this.mic);
    const node = new AudioWorkletNode(ctx, "pcm16-capture");
    node.port.onmessage = (e) => {
      if (!this.muted && this.ws?.readyState === WebSocket.OPEN) this.ws.send(e.data as ArrayBuffer);
    };
    // The worklet must be pulled by the graph to run; a silent sink keeps it alive without echoing the mic.
    const sink = ctx.createGain();
    sink.gain.value = 0;
    source.connect(node).connect(sink).connect(ctx.destination);
  }

  private handle(msg: { type: string; [key: string]: unknown }): void {
    switch (msg.type) {
      case "status":
        this.on.onState(msg.state as CallState);
        break;
      case "ice":
        this.connectAvatar(msg.servers as IceServer[]).catch(() =>
          this.on.onError("Could not connect to the avatar video stream."));
        break;
      case "avatar.answer":
        this.pc?.setRemoteDescription(JSON.parse(atob(msg.sdp as string)) as RTCSessionDescriptionInit)
          .catch(() => this.on.onError("The avatar video stream was rejected."));
        break;
      case "caption":
        this.on.onCaption(msg.role as "user" | "assistant", msg.text as string, Boolean(msg.final));
        break;
      case "tool":
        this.on.onTool(msg.name as string, msg.state as ToolState);
        break;
      case "error":
        this.on.onError(msg.message as string);
        break;
      case "ended":
        this.finish(msg.reason as string);
        break;
    }
  }

  private async connectAvatar(servers: IceServer[]): Promise<void> {
    const pc = new RTCPeerConnection({
      iceServers: servers.map((s) => ({ urls: s.urls, username: s.username, credential: s.credential })),
    });
    this.pc = pc;
    pc.ontrack = (e) => {
      const stream = e.streams[0] ?? new MediaStream([e.track]);
      if (e.track.kind === "video") this.video = stream;
      else this.audio = stream;
      this.on.onMedia(this.video, this.audio);
    };
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed") this.on.onError("The avatar video connection failed. Check your network.");
    };
    // The avatar service expects both media lines and a data channel in the offer, as in Microsoft's sample.
    pc.addTransceiver("video", { direction: "sendrecv" });
    pc.addTransceiver("audio", { direction: "sendrecv" });
    pc.createDataChannel("eventChannel");
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise<void>((resolve) => {
      if (pc.iceGatheringState === "complete") return resolve();
      const timer = setTimeout(resolve, 2000);
      pc.addEventListener("icegatheringstatechange", () => {
        if (pc.iceGatheringState === "complete") {
          clearTimeout(timer);
          resolve();
        }
      });
    });
    this.send({ type: "avatar.offer", sdp: btoa(JSON.stringify(pc.localDescription)) });
  }

  private finish(reason: string, isError = false): void {
    if (this.ended) return;
    this.ended = true;
    if (isError) this.on.onError(reason);
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) this.ws.close();
    this.pc?.close();
    this.mic?.getTracks().forEach((t) => t.stop());
    this.ctx?.close().catch(() => undefined);
    this.on.onMedia(null, null);
    this.on.onState("idle");
    this.on.onEnded(reason);
  }
}
