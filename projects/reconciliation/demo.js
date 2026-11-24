// Synthetic examples. All monetary values are integer cents.
const cases = [
  {party:"纸舟样例社",role:"供应商",side:"应付",invoice:"DEMO-INV-01",invoiceDate:"2026-04-02",gross:126000,bank:"DEMO-BANK-01",bankDate:"2026-04-12",cash:96000,flow:"支出"},
  {party:"方格样例社",role:"客户",side:"应收",invoice:"DEMO-INV-02",invoiceDate:"2026-04-04",gross:188000,bank:"DEMO-BANK-02",bankDate:"2026-04-15",cash:188000,flow:"收入"}
];
const paid = new Set();
let active = 0;
const money = new Intl.NumberFormat("zh-CN",{style:"currency",currency:"CNY"});
const format = cents => money.format(cents/100);
const el = id => document.getElementById(id);

function record(title, rows) {
  const panel = document.createElement("article");
  panel.className = "record";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const list = document.createElement("dl");
  for (const [label,value] of rows) {
    const line = document.createElement("div");
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    term.textContent = label;
    detail.textContent = value;
    if (label==="金额") detail.className = "amount";
    line.append(term,detail);
    list.append(line);
  }
  panel.append(heading,list);
  return panel;
}

function render() {
  const item = cases[active];
  const confirmed = paid.has(active);
  const allocated = confirmed ? item.cash : 0;
  const outstanding = item.gross - allocated;
  for (const [i,button] of [...el("parties").children].entries()) {
    button.setAttribute("aria-pressed", String(active===i));
  }
  el("role").textContent = item.role;
  el("party").textContent = item.party;
  el("status").textContent = !confirmed ? "待核对" : outstanding ? "部分核销" : "已结清";
  el("status").classList.toggle("done",confirmed);
  el("records").replaceChildren(
    record(item.side+"发票",[["发票编号",item.invoice],["开票日期",item.invoiceDate],["金额",format(item.gross)]]),
    record("银行"+item.flow,[["流水编号",item.bank],["交易日期",item.bankDate],["金额",format(item.cash)]])
  );
  el("gross").textContent = format(item.gross);
  el("applied").textContent = format(allocated);
  el("balance-label").textContent = item.side+"余额";
  el("balance").textContent = format(outstanding);
  el("hint").textContent = confirmed
    ? "本次已核销 "+format(item.cash)+"，"+item.side+"余额由 "+format(item.gross)+" 变为 "+format(outstanding)+"。"
    : "本次可核销 "+format(item.cash)+"，请核对单据后确认。";
  el("confirm").disabled = confirmed;
  el("confirm").textContent = confirmed ? "已确认核销" : "确认核销";
}

cases.forEach((item,index) => {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "party-choice";
  const role = document.createElement("small");
  role.textContent = item.role;
  button.append(role,document.createTextNode(item.party));
  button.onclick = () => {active=index;render();};
  el("parties").append(button);
});
el("confirm").onclick = () => {paid.add(active);render();};
el("reset").onclick = () => {paid.clear();active=0;render();};
render();
