"use strict";

const $ = (id) => document.getElementById(id);
const key = "carton-label-public-demo-v1";
const stageNames = ["待登记", "已打印", "已入库", "已出库"];
const historyNames = ["分箱生成", "打印登记", "扫码入库", "扫码出库"];

function splitOrder(qty, capacity) {
  if (![qty, capacity].every(n => Number.isInteger(n) && n >= 1 && n <= 99999)) {
    throw Error("请输入 1 至 99999 之间的整数。");
  }
  const count = Math.ceil(qty / capacity);
  if (count > 999) throw Error("单次演示最多支持 999 箱，请调整每箱装量。");
  return Array.from({length: count}, (_, i) => Math.min(capacity, qty - i * capacity));
}

// EAN-13 of an internal, fictional order. The thirteenth digit is its checksum.
function boxCode(index) {
  const body = "200042001" + String(index + 1).padStart(3, "0");
  const sum = [...body].reduce((v, digit, i) => v + Number(digit) * (i % 2 ? 3 : 1), 0);
  return body + (10 - sum % 10) % 10;
}

function makeOrder(qty, capacity) {
  const now = new Date().toISOString();
  return {qty, capacity, boxes: splitOrder(qty, capacity).map(units => ({units, times: [now]}))};
}

function loadOrder() {
  try {
    const s = JSON.parse(localStorage.getItem(key));
    const quantities = splitOrder(s.qty, s.capacity);
    if (s.boxes.length !== quantities.length) throw Error();
    if (s.boxes.every((b, i) => b.units === quantities[i] && Array.isArray(b.times)
      && b.times.length >= 1 && b.times.length <= 4
      && b.times.every(t => typeof t === "string" && !Number.isNaN(Date.parse(t))))) return s;
  } catch { /* Invalid or unavailable storage starts a new demonstration. */ }
  return makeOrder(250, 100);
}

let order = loadOrder();
let selected = 0;
function message(text, error = false) {
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
}
function save(text) {
  try {
    localStorage.setItem(key, JSON.stringify(order));
    message(text);
  } catch {
    message(text + " 浏览器未允许保存，当前操作仅在本页保留。");
  }
  render();
}

// EAN-13 left/right parity encodings. The nine-module white margins flank the bars.
function barcodeSvg(value) {
  const digit = ["0001101", "0011001", "0010011", "0111101", "0100011",
    "0110001", "0101111", "0111011", "0110111", "0001011"];
  const parity = ["LLLLLL","LLGLGG","LLGGLG","LLGGGL","LGLLGG",
    "LGGLLG","LGGGLL","LGLGLG","LGLGGL","LGGLGL"][Number(value[0])];
  const invert = s => [...s].map(c => c === "0" ? "1" : "0").join("");
  let bits = "101";
  for (let i = 1; i <= 6; i++) {
    const left = digit[Number(value[i])];
    bits += parity[i - 1] === "L" ? left : [...invert(left)].reverse().join("");
  }
  bits += "01010";
  for (let i = 7; i <= 12; i++) bits += invert(digit[Number(value[i])]);
  bits += "101";
  const bars = [...bits].map((bit,i) => bit === "1"
    ? '<rect x="' + (i + 12) + '" width="1" height="26"/>' : "").join("");
  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 119 26" role="img" aria-label="EAN-13 箱码" shape-rendering="crispEdges">' + bars + '</svg>';
}

function render() {
  const {qty, capacity, boxes} = order;
  const locked = boxes.some(b => b.times.length > 1);
  $("qty").value = qty; $("capacity").value = capacity;
  for (const element of $("split").elements) element.disabled = locked;
  $("register").disabled = locked;
  $("total").textContent = boxes.length + " 箱 / " + qty + " 件";
  const rows = boxes.map((box,i) => {
    const row = document.createElement("tr");
    if (i === selected) row.classList.add("selected");
    const position = String(i + 1).padStart(3,"0");
    for (const val of [position, box.units + " 件", box.units === capacity ? "满箱" : "尾箱"]) {
      row.insertCell().textContent = val;
    }
    const tag = document.createElement("span");
    tag.className = "tag"; tag.textContent = stageNames[box.times.length - 1];
    row.insertCell().append(tag);
    const button = document.createElement("button");
    button.type = "button"; button.className = "link"; button.textContent = "查看";
    button.setAttribute("aria-label", "查看第 " + position + " 箱");
    button.onclick = () => { selected = i; render(); message("已将所选箱码填入扫码框。"); };
    row.insertCell().append(button);
    return row;
  });
  $("rows").replaceChildren(...rows);
  const box = boxes[selected], code = boxCode(selected);
  $("code").value = code;
  $("position").textContent = (selected + 1) + " / " + boxes.length;
  $("units").textContent = box.units + " PCS";
  $("barcode").innerHTML = barcodeSvg(code);
  $("digits").textContent = code;
  $("selected-box").textContent = "第 " + (selected + 1) + " 箱";
  const events = box.times.map((stamp, index) => {
    const item = document.createElement("li"), time = document.createElement("time");
    time.dateTime = stamp;
    time.textContent = new Date(stamp).toLocaleTimeString("zh-CN",{hour12:false});
    item.append(historyNames[index], time);
    return item;
  });
  $("events").replaceChildren(...events);
}

$("split").onsubmit = event => {
  event.preventDefault();
  if (order.boxes.some(b => b.times.length > 1)) return;
  try {
    order = makeOrder(Number($("qty").value), Number($("capacity").value));
    selected = 0; save("分箱清单已生成，请核对数量并登记打印。");
  } catch (err) { message(err.message, true); }
};
$("register").onclick = () => {
  if (order.boxes.some(b => b.times.length > 1)) return;
  const now = new Date().toISOString();
  order.boxes.forEach(b => b.times.push(now));
  save("已登记 " + order.boxes.length + " 箱，箱码可用于入库。");
};
$("paper").onclick = () => window.print();
$("scan").onsubmit = event => {
  event.preventDefault();
  const code = $("code").value.trim();
  const i = order.boxes.findIndex((_,index) => boxCode(index) === code);
  if (i === -1) { message("未找到此箱码，请核对当前订单箱贴。", true); return; }
  const action = $("operation").value, box = order.boxes[i];
  if (action === "lookup") {
    selected = i; render(); message("已显示所查箱码的全部流转记录。"); return;
  }
  const stage = box.times.length - 1;
  if (stage === 0) { message("请先登记本批打印。", true); return; }
  if (action === "out" && stage === 1) { message("此箱尚未入库。", true); return; }
  if ((action === "in" && stage >= 2) || stage === 3) {
    message("该操作已登记，请勿重复扫描。", true); return;
  }
  box.times.push(new Date().toISOString());
  selected = i; save("第 " + (i + 1) + " 箱" + stageNames[box.times.length - 1] + "。");
};
$("reset").onclick = () => {
  if (!window.confirm("清除本浏览器的演示记录并恢复示例订单？")) return;
  order = makeOrder(250,100); selected = 0; save("已恢复示例订单。");
};
render();
