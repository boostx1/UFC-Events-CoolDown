document.getElementById('event-name').textContent = "TEST UFC Event";
let time = 10; // 10 seconds countdown for quick test
const countdownEl = document.getElementById('countdown');

const interval = setInterval(() => {
  countdownEl.textContent = time + "s";
  time--;
  if(time < 0) {
    clearInterval(interval);
    countdownEl.textContent = "🔥 It's fight time!";
  }
}, 1000);
