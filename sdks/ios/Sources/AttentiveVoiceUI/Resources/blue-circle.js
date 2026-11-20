const fragment = `
// === CONSTANTS ===
const float GOLDEN_ANGLE = 2.3999632;
const float TAU = 6.28318530;

// === PCG hash - https://www.jcgt.org/published/0009/03/02/
uvec3 hash3(uvec3 v) {
    v = v * 1664525u + 1013904223u;
    v.x += v.y * v.z;
    v.y += v.z * v.x;
    v.z += v.x * v.y;
    v ^= v >> 16u;
    v.x += v.y * v.z;
    v.y += v.z * v.x;
    v.z += v.x * v.y;
    return v;
}

// Seed
vec3 seedRandom(float seedVal) {
    uvec3 s = uvec3(
        floatBitsToUint(seedVal),
        floatBitsToUint(seedVal * 1.5 + 7.31),
        floatBitsToUint(seedVal * 2.7 + 13.37)
    );
    s = hash3(s);
    return vec3(s) / float(0xFFFFFFFFu);
}

// === COLOR SPACE UTILITIES ===
vec3 toLinear(vec3 c) {
    return pow(c, vec3(2.2));
}

vec3 toSrgb(vec3 c) {
    return pow(clamp(c, 0.0, 1.0), vec3(0.4545));
}

vec3 linearToOklab(vec3 c) {
    float l = 0.4122214708 * c.r + 0.5363325363 * c.g + 0.0514459929 * c.b;
    float m = 0.2119034982 * c.r + 0.6806995451 * c.g + 0.1073969566 * c.b;
    float s = 0.0883024619 * c.r + 0.2817188376 * c.g + 0.6299787005 * c.b;
    
    l = pow(max(l, 0.0), 1.0/3.0);
    m = pow(max(m, 0.0), 1.0/3.0);
    s = pow(max(s, 0.0), 1.0/3.0);
    
    return vec3(
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    );
}

vec3 oklabToLinear(vec3 c) {
    float l = c.x + 0.3963377774 * c.y + 0.2158037573 * c.z;
    float m = c.x - 0.1055613458 * c.y - 0.0638541728 * c.z;
    float s = c.x - 0.0894841775 * c.y - 1.2914855480 * c.z;
    
    l = l * l * l;
    m = m * m * m;
    s = s * s * s;
    
    return vec3(
        +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    );
}

vec3 oklabToLch(vec3 lab) {
    return vec3(lab.x, length(lab.yz), atan(lab.z, lab.y));
}

vec3 lchToOklab(vec3 lch) {
    return vec3(lch.x, lch.y * cos(lch.z), lch.y * sin(lch.z));
}

vec3 mixLch(vec3 lab0, vec3 lab1, float t) {
    vec3 lch0 = oklabToLch(lab0);
    vec3 lch1 = oklabToLch(lab1);
    
    if (lch0.y < 0.05) lch0.z = lch1.z;
    if (lch1.y < 0.05) lch1.z = lch0.z;
    
    float dh = lch1.z - lch0.z;
    if (dh > 3.14159265) dh -= 6.28318530;
    if (dh < -3.14159265) dh += 6.28318530;
    
    return lchToOklab(vec3(
        mix(lch0.x, lch1.x, t),
        mix(lch0.y, lch1.y, t),
        lch0.z + dh * t
    ));
}

// === PALETTE SAMPLING ===
vec3 getColor(int idx) {
    if (u_colors_length < 1) return vec3(0.0);
    int safeIdx = clamp(idx, 0, u_colors_length - 1);
    return u_colors[safeIdx].rgb;
}

vec3 paletteN(float t, int count) {
    if (count < 1) return vec3(0.0);
    if (count < 2) return toLinear(getColor(0));
    
    float segmentSize = 1.0 / float(count - 1);
    t = clamp(t, 0.0, 1.0);
    int idx = min(int(floor(t / segmentSize)), count - 2);
    float localT = clamp((t - float(idx) * segmentSize) / segmentSize, 0.0, 1.0);
    
    vec3 lab0 = linearToOklab(toLinear(getColor(idx)));
    vec3 lab1 = linearToOklab(toLinear(getColor(idx + 1)));
    
    return oklabToLinear(mixLch(lab0, lab1, localT));
}

// === DITHER ===
float IGN(vec2 uv) {
    return fract(52.9829189 * fract(dot(uv, vec2(0.06711056, 0.00583715))));
}

float quickNoise(vec2 I) {
    return fract(sin(dot(I, vec2(12.9898, 78.233))) * 43758.5453);
}

// Dither Mode: 0=Off, 1=IGN, 2=quickNoise
float getDither(vec2 I, float mode) {
    if (mode < 0.5) return 0.5;          // 0: Off
    if (mode < 1.5) return IGN(I);       // 1: Smooth
    return quickNoise(I);                // 2: Grain
}

// === POST-PROCESS ===
vec3 softGamutMap(vec3 linearRgb) {
    float maxC = max(linearRgb.r, max(linearRgb.g, linearRgb.b));
    float minC = min(linearRgb.r, min(linearRgb.g, linearRgb.b));
    
    if (minC >= 0.0 && maxC <= 1.0) return linearRgb;
    
    vec3 lab = linearToOklab(max(linearRgb, 0.0));
    float L = clamp(lab.x, 0.0, 1.0);
    float C = length(lab.yz);
    float h = atan(lab.z, lab.y);
    
    float maxChroma = 0.4 * (1.0 - pow(abs(2.0 * L - 1.0), 2.0));
    
    if (C > maxChroma * 0.7) {
        float knee = maxChroma * 0.7;
        C = knee + (maxChroma - knee) * tanh(min((C - knee) / (maxChroma - knee + 0.001), 6.0));
    }
    
    return clamp(oklabToLinear(vec3(L, C * cos(h), C * sin(h))), 0.0, 1.0);
}

vec3 applyContrastSaturation(vec3 linearRgb, float contrast, float saturation) {
    vec3 lab = linearToOklab(linearRgb);
    float C = length(lab.yz);
    float h = atan(lab.z, lab.y);
    
    lab.x = clamp((lab.x - 0.5) * contrast + 0.5, 0.0, 1.0);
    C *= saturation;
    lab.y = C * cos(h);
    lab.z = C * sin(h);
    
    return oklabToLinear(lab);
}

// === MAIN ===
void main() {
    vec2 fragCoord = v_uv * u_resolution;
    vec2 r = u_resolution;
    vec2 p = (fragCoord * 2.0 - r) / r.y;
    
    int colorCount = u_colors_length;
    
    // Early out: no colors -> black
    if (colorCount < 1) {
        fragColor = vec4(0.0, 0.0, 0.0, 1.0);
        return;
    }

    float t = u_time * 0.3;
    
    // Map time onto a circle so animation seamlessly wraps.
    float looping = step(0.5, u_loop);
    float phase = TAU * u_time / max(u_loop, 0.01);
    float radius = u_loop * u_speed * 0.3 / TAU;
    float tA = sin(phase) * radius;
    float tB = (1.0 - cos(phase)) * radius;
    
    // Seed-based offsets
    vec3 seedOffset = seedRandom(u_seed);
    vec3 seedOffset2 = seedRandom(u_seed + 100.0);
    
    // Golden angle rotation
    float seedAngle = u_seed * GOLDEN_ANGLE;
    vec2 seedPhase = (seedOffset2.xy - 0.5) * TAU;
    
    // Seed-based rotation
    float cs = cos(seedAngle);
    float sn = sin(seedAngle);
    p = mat2(cs, -sn, sn, cs) * p;
    
    // Get dither value
    float dither = getDither(floor(fragCoord / u_pixelRatio), u_ditherMode);
    
    // === TURBULENCE ===
    float totalVal = 0.0;
    float totalWeight = 0.0;
    int turbIter = int(u_turbIter);
    
    float freq = 1.0 / max(u_turbFreq, 0.01);
    
    for (float i = 0.0; i < 4.0; i++) {
        float eph = i / 4.0;
       
        vec2 q = p * u_scale;
        float sq = eph * eph;
        
        if (u_jellify > 0.5) {
            q.yx *= mix(1.0, 0.5, 1.0 - exp(-sq));
        }
        
        float a = seedPhase.x;
        float d = seedPhase.y;
        
        for (int j = 2; j < 13; j++) {
            if (j >= turbIter) break;
            float fj = float(j);
            // When looping, use circular time. Otherwise original t.
            float t1 = mix(t * u_speed, tA, looping);
            float t2 = mix(t * u_speed, tB, looping);
            q += u_turbAmp * sin(q.yx / freq * fj + t1 + vec2(a, d) + seedOffset.xy * fj) / fj;
            a += cos(fj + d * 1.2 + q.x * 2.0 - t1 + seedOffset2.z + t2 * 0.3 * looping);
            d += sin(fj * q.y + a + seedOffset.z + t1 + seedOffset2.y + t2 * 0.3 * looping);
        }
        
        float v = 0.5 + 0.5 * sin(length(q.yx + vec2(a, d) * 0.2) * u_waveFreq + i * i + seedOffset.x);
        float weight = smoothstep(0.0, 0.5, eph) * smoothstep(1.0, 0.5, eph);
        totalVal += v * weight;
        totalWeight += weight;
    }
    
    float val = totalVal / totalWeight;
    val = clamp((val - 0.3) / 0.4, 0.0, 1.0);
    val = pow(val, exp(-u_distBias));
    val = clamp(val + (dither - 0.5) * u_dither, 0.0, 1.0);
    
    vec3 col = paletteN(val, colorCount);
    col *= u_exposure;
    col = applyContrastSaturation(col, u_contrast, u_saturation);
    col = softGamutMap(col);
    col = toSrgb(col);
    
    fragColor = vec4(col, 1.0);
}
`;

// A self-contained WebGL2 wrapper around the published Liquid Gradient shader.
export class BlueCircle extends HTMLElement {
  static get observedAttributes() { return ["colors", "texture", "mode"]; }

  attributeChangedCallback(name) {
    if (!this.canvas) return;
    if (name === "colors") this.applyColors();
    if (name === "texture") this.applyTexture();
    if (name === "mode" && this.getAttribute("mode") !== "talking") this.intensity = 0;
  }

  // Supply a normalized audio level between 0 (silence) and 1 (loud).
  set intensity(value) {
    this.targetIntensity = Number.isFinite(Number(value)) ? Math.max(0, Math.min(1, Number(value))) : 0;
  }

  get intensity() { return this.targetIntensity || 0; }

  applyTexture() {
    const smooth = this.getAttribute("texture") === "smooth";
    this.shadowRoot.querySelector("img").style.display = this.originalFallback && !smooth ? "block" : "none";
    if (!this.program) return;
    this.gl.useProgram(this.program);
    this.gl.uniform1f(this.gl.getUniformLocation(this.program, "u_ditherMode"), smooth ? 0 : 2);
    this.gl.uniform1f(this.gl.getUniformLocation(this.program, "u_dither"), smooth ? 0 : 0.2);
    this.draw();
  }

  applyColors() {
    const defaults = ["#0263d9", "#65a6eb", "#9cc0f0"];
    const supplied = (this.getAttribute("colors") || "").split(",").map(color => color.trim());
    const colors = supplied.length === 3 && supplied.every(color => /^#[0-9a-f]{6}$/i.test(color))
      ? supplied : defaults;
    const values = colors.flatMap(color => [
      parseInt(color.slice(1, 3), 16) / 255,
      parseInt(color.slice(3, 5), 16) / 255,
      parseInt(color.slice(5, 7), 16) / 255,
      1
    ]);
    this.shadowRoot.querySelector(".frame").style.background = `radial-gradient(circle at 30% 30%, ${colors[2]}, ${colors[1]} 45%, ${colors[0]} 100%)`;
    this.originalFallback = colors === defaults;
    this.shadowRoot.querySelector("img").style.display = this.originalFallback && this.getAttribute("texture") !== "smooth" ? "block" : "none";
    if (!this.program) return;
    this.gl.useProgram(this.program);
    this.gl.uniform4fv(this.gl.getUniformLocation(this.program, "u_colors[0]"), new Float32Array(values));
    this.draw();
  }

  connectedCallback() {
    if (this.canvas) return;
    const root = this.shadowRoot || this.attachShadow({ mode: "open" });
    root.innerHTML = `<style>:host{display:inline-block;width:200px;aspect-ratio:1;border-radius:50%;overflow:hidden;background:#9cc0f0} canvas,img{display:block;width:100%;height:100%;object-fit:cover} img{position:absolute} .frame{position:relative;width:100%;height:100%} canvas{position:relative;visibility:hidden}</style><div class="frame"><img alt="" src="${new URL('./fallback.png', import.meta.url)}"><canvas aria-hidden="true"></canvas></div>`;
    this.canvas = root.querySelector("canvas");
    this.applyColors();
    const gl = this.canvas.getContext("webgl2", { alpha: false, antialias: false });
    if (!gl) return;
    this.gl = gl;
    const vertex = `#version 300 es
      in vec2 position;
      out vec2 v_uv;
      void main() { v_uv = position * 0.5 + 0.5; gl_Position = vec4(position, 0.0, 1.0); }`;
    const header = `#version 300 es
      precision highp float;
      precision highp int;
      in vec2 v_uv;
      out vec4 fragColor;
      uniform vec2 u_resolution;
      uniform float u_time, u_pixelRatio, u_seed, u_speed, u_loop, u_scale;
      uniform float u_turbAmp, u_turbFreq, u_turbIter, u_waveFreq, u_distBias;
      uniform float u_jellify, u_ditherMode, u_dither, u_exposure, u_contrast, u_saturation;
      uniform vec4 u_colors[8];
      uniform int u_colors_length;
    `;
    const compile = (type, source) => {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        console.error("Blue circle shader", gl.getShaderInfoLog(shader));
        gl.deleteShader(shader);
        return null;
      }
      return shader;
    };
    const vs = compile(gl.VERTEX_SHADER, vertex);
    const fs = compile(gl.FRAGMENT_SHADER, header + fragment);
    if (!vs || !fs) return;
    const program = gl.createProgram();
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      console.error("Blue circle program", gl.getProgramInfoLog(program));
      return;
    }
    gl.useProgram(program);
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, "position");
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    this.program = program;
    const set = (name, value) => gl.uniform1f(gl.getUniformLocation(program, name), value);
    for (const [name, value] of Object.entries({u_seed:653, u_speed:1.28, u_loop:0, u_scale:1.32, u_turbAmp:1, u_turbFreq:0.1, u_turbIter:5, u_waveFreq:3.8, u_distBias:0, u_jellify:1, u_ditherMode:2, u_dither:0.2, u_exposure:1.1, u_contrast:1.1, u_saturation:1})) set(name, value);
    gl.uniform1i(gl.getUniformLocation(program, "u_colors_length"), 3);
    this.timeLocation = gl.getUniformLocation(program, "u_time");
    this.resolutionLocation = gl.getUniformLocation(program, "u_resolution");
    this.pixelRatioLocation = gl.getUniformLocation(program, "u_pixelRatio");
    this.elapsed = 0;
    this.smoothedIntensity = 0;
    this.targetIntensity = 0;
    this.orbScale = 1;
    this.scaleVelocity = 0;
    this.applyColors();
    this.applyTexture();
    this.visible = false;
    this.motion = matchMedia("(prefers-reduced-motion: reduce)");
    this.onMotion = () => this.update();
    this.onVisibility = () => this.update();
    this.motion.addEventListener("change", this.onMotion);
    document.addEventListener("visibilitychange", this.onVisibility);
    this.resizeObserver = new ResizeObserver(() => { this.resize(); this.draw(); });
    this.resizeObserver.observe(this);
    this.observer = new IntersectionObserver(entries => { this.visible = entries[0].isIntersecting; this.update(); });
    this.observer.observe(this);
    this.resize();
    this.draw();
    this.canvas.style.visibility = "visible";
  }
  resize() {
    const ratio = Math.min(devicePixelRatio || 1, 2);
    this.canvas.width = Math.max(1, Math.round(this.clientWidth * ratio));
    this.canvas.height = Math.max(1, Math.round(this.clientHeight * ratio));
    this.gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    this.gl.uniform2f(this.resolutionLocation, this.canvas.width, this.canvas.height);
    this.gl.uniform1f(this.pixelRatioLocation, ratio);
  }
  draw() {
    this.gl.uniform1f(this.timeLocation, this.elapsed / 1000);
    this.gl.drawArrays(this.gl.TRIANGLE_STRIP, 0, 4);
  }
  update() {
    cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.lastTime = null;
    if (!this.visible || document.hidden || this.motion.matches || this.nativePaused) {
      this.smoothedIntensity = 0;
      this.orbScale = 1;
      this.scaleVelocity = 0;
      this.style.transform = "";
      this.canvas.style.transform = "";
      return;
    }
    const tick = time => {
      if (this.lastTime !== null) {
        const delta = Math.min(time - this.lastTime, 100);
        const mode = this.getAttribute("mode");
        const target = mode === "talking" ? this.intensity : 0;
        const response = target > this.smoothedIntensity ? 70 : 240;
        this.smoothedIntensity += (target - this.smoothedIntensity) * (1 - Math.exp(-delta / response));
        const rate = 0.28 + 0.27 * Math.min(1, this.smoothedIntensity / 0.1) + this.smoothedIntensity * 3;
        this.elapsed += delta * rate;
        this.canvas.style.transform = `scale(${1 + this.smoothedIntensity * 0.09})`;

        // Drive both layers from the same smoothed audio and display clock.
        // Advance the web caller's spring in small steps after a slower frame.
        const targetScale = 1 + this.smoothedIntensity * 0.25;
        const steps = Math.max(1, Math.ceil(delta * 0.12));
        const stepSeconds = delta / (1000 * steps);
        for (let i = 0; i < steps; i++) {
          const acceleration = (300 * (targetScale - this.orbScale) - 22 * this.scaleVelocity) / 0.68;
          this.scaleVelocity += acceleration * stepSeconds;
          this.orbScale += this.scaleVelocity * stepSeconds;
        }
        this.style.transform = `scale(${this.orbScale})`;
      }
      this.lastTime = time;
      this.draw();
      this.frame = requestAnimationFrame(tick);
    };
    this.frame = requestAnimationFrame(tick);
  }
  disconnectedCallback() {
    cancelAnimationFrame(this.frame);
    this.observer?.disconnect();
    this.resizeObserver?.disconnect();
    this.motion?.removeEventListener("change", this.onMotion);
    document.removeEventListener("visibilitychange", this.onVisibility);
    // Allow the same element to be attached again.
    if (this.shadowRoot) this.shadowRoot.innerHTML = "";
    this.canvas = null;
    this.program = null;
  }
}
if (!customElements.get("blue-circle")) customElements.define("blue-circle", BlueCircle);
