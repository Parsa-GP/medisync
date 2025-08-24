async function atp_get() {
	let res = await fetch('/api/autoplay_get');
	console.log(res);
	let data = await res.json();
	return data
}
const atp = document.getElementById("autoplay-toggle");

console.log(atp_get());
atp.addEventListener("change", e => {
	fetch('/api/autoplay_set', {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json'
		},
		body: JSON.stringify( e.target.checked )
	});
});
let musics = {};
							
function fancyname(name, hash, duration) {
	dur_sec = Math.round(duration%60).toString().padStart(2,'0')
	dur_min = Math.round(duration/40).toString().padStart(2,'0')
	return name+" ("+hash.slice(0,8)+") ["+dur_min+":"+dur_sec+"]";
}
async function loadMusics() {
	let res = await fetch('/api/musics');
	let data = await res.json();
	const container = document.getElementById("music-list");
	container.innerHTML = "";
	for (const [h, music_info] of Object.entries(data)) {
		let div = document.createElement("div");
		div.className = "music-item";
		div.textContent = fancyname(music_info.name, h, music_info.duration);
		div.onclick = async () => { await fetch('/api/queue', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({hash:h})}); refreshQueue(); };
		container.appendChild(div);
	}
	musics = data;
}

async function refreshQueue() {
  let res = await fetch('/api/queue');
  let data = await res.json();
  const container = document.getElementById("queue-list");
  container.innerHTML = "";
  data.forEach((h, i) => {
	  let div = document.createElement("div");
	  div.className = "queue-item";
	  div.draggable = true;
	  div.dataset.index = i;
	  div.textContent = fancyname(musics[h].name, h, musics[h].duration);

	  let btn = document.createElement("button");
	  btn.textContent = "Delete";
	  btn.onclick = async (e) => { 
		  e.stopPropagation();
		  await fetch('/api/queue', {method:'DELETE', headers:{'Content-Type':'application/json'}, body: JSON.stringify({hash:h})}); 
		  refreshQueue(); 
	  };
	  div.appendChild(btn);

	  div.addEventListener('dragstart', (e)=>{div.classList.add('dragging'); e.dataTransfer.setData('text/plain', i);});
	  div.addEventListener('dragend', (e)=>{div.classList.remove('dragging');});
	  div.addEventListener('dragover', e=>e.preventDefault());
	  div.addEventListener('drop', async e=>{
		  e.preventDefault();
		  let from = parseInt(e.dataTransfer.getData('text/plain'));
		  let to = parseInt(div.dataset.index);
		  await fetch('/api/queue/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({from,to})});
		  refreshQueue();
	  });

	  container.appendChild(div);
  });

  // Update now playing
  let npRes = await fetch('/api/current');
  let npData = await npRes.json();

  //console.log(npData)
  //console.log(musics)

  const npElement = document.getElementById("now-playing");
  const title = document.querySelector("title");

	npElement.textContent = npData.hash ? fancyname(npData.hash, musics[npData.hash].name, musics[npData.hash].duration)  : "Nothing";
	title = "MusiSync - " + npData.hash ? musics[npData.hash].name : "Nothing"
}


async function play() { await fetch('/api/play', {method:'POST'}); refreshQueue(); }
async function pause() { await fetch('/api/pause', {method:'POST'}); refreshQueue(); }

loadMusics();
refreshQueue();
