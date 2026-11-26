// Watch actual media operations. Only the stalled-startup scenario overrides resume.
window.voices = [];
window.contexts = [];
window.starts = 0;
window.resumes = 0;
window.seeks = [];
window.Audio = new Proxy(Audio, {
  construct(target, args) {
    const element = Reflect.construct(target, args);
    voices.push(element);
    const play = element.play.bind(element);
    element.play = () => { starts++; return play(); };
    const clock = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "currentTime");
    Object.defineProperty(element, "currentTime", {
      get() { return clock.get.call(this); },
      set(next) {
        seeks.push({element, delta: next - clock.get.call(this)});
        clock.set.call(this, next);
      },
    });
    return element;
  },
});
window.AudioContext = class extends AudioContext {
  constructor(...args) { super(...args); contexts.push(this); }
  resume() {
    resumes++;
    return location.search === "?stalled" ? new Promise(() => {}) : super.resume();
  }
};
window.stems = () => voices.filter(element => element.hasAttribute("src"));
