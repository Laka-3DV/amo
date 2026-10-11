export type Appearance = "blue" | "aurora" | "pixel" | "paper" | "native" | "terminal" | "engineering" | "pixel-workbench";
export type InterfaceLayout = "classic" | "native" | "terminal" | "engineering" | "pixel";
export const APPEARANCE_KEY = "dsh_remote_appearance";
export const APPEARANCES: { id: Appearance; name: string; description: string; colors: string[]; darkColors: string[]; layout?: InterfaceLayout }[] = [
  {id:"native",name:"原生精简",description:"紧凑输入、轻量列表与蓝色留白",layout:"native",colors:["#F8F9FC","#4B6EEB","#E6EAF1"],darkColors:["#171B24","#A2B5FF","#353E4E"]},
  {id:"terminal",name:"Terminal 工作台",description:"提示符、输出分区与等宽工具栏",layout:"terminal",colors:["#ECF1F5","#277D78","#CADBE2"],darkColors:["#101923","#78CFC0","#314656"]},
  {id:"engineering",name:"工程对话",description:"黑白层次、胶囊输入与简洁图标",layout:"engineering",colors:["#FAFAFA","#303438","#DFE1E2"],darkColors:["#111315","#F3F4F5","#36393C"]},
  {id:"pixel-workbench",name:"像素工作台",description:"纸色像素边框、消息标签与方形控件",layout:"pixel",colors:["#F5F0E4","#366963","#B9B6A5"],darkColors:["#232B31","#8DC9BB","#5A6D78"]},
  {id:"blue",name:"DeepSeek 蓝",description:"清透、轻盈，留白与蓝色",colors:["#F8FAFF","#4D6BFE","#CCD7FF"],darkColors:["#191E2B","#8A9EFF","#3D4965"]},
  {id:"aurora",name:"紫青极光",description:"柔和光晕，紫色与薄荷青",colors:["#F8F5FF","#7357E5","#83D7CE"],darkColors:["#252034","#B69FFF","#77CCBD"]},
  {id:"pixel",name:"像素游戏机",description:"像素纹理、利落描边与跳色",colors:["#FFF8E8","#5957BF","#32958C"],darkColors:["#1D2B3E","#66CFCD","#ED92AC"]},
  {id:"paper",name:"暖纸",description:"温暖纸色，安静阅读",colors:["#F5F0E6","#AA5940","#D9C6A4"],darkColors:["#2B2722","#DDA57A","#72513C"]},
];
export function readAppearance(storage: Pick<Storage,"getItem">): Appearance {
  try {
    const value=storage.getItem(APPEARANCE_KEY);
    return APPEARANCES.some(item=>item.id===value) ? value as Appearance : "blue";
  } catch { return "blue"; }
}

export function appearanceLayout(appearance?: Appearance): InterfaceLayout {
  return APPEARANCES.find(item => item.id === appearance)?.layout ?? "classic";
}
