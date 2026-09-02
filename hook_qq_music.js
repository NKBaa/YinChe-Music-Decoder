const TARGET_DLL = "QQMusicCommon.dll";

function requiredExport(name) {
  const address = Module.findExportByName(TARGET_DLL, name);
  if (address === null) throw new Error("QQMusicCommon.dll 接口不兼容: " + name);
  return address;
}

const construct = new NativeFunction(requiredExport("??0EncAndDesMediaFile@@QAE@XZ"), "pointer", ["pointer"], "thiscall");
const destruct = new NativeFunction(requiredExport("??1EncAndDesMediaFile@@QAE@XZ"), "void", ["pointer"], "thiscall");
const openFile = new NativeFunction(requiredExport("?Open@EncAndDesMediaFile@@QAE_NPB_W_N1@Z"), "bool", ["pointer", "pointer", "bool", "bool"], "thiscall");
const getSize = new NativeFunction(requiredExport("?GetSize@EncAndDesMediaFile@@QAEKXZ"), "uint32", ["pointer"], "thiscall");
const readFile = new NativeFunction(requiredExport("?Read@EncAndDesMediaFile@@QAEKPAEK_J@Z"), "uint", ["pointer", "pointer", "uint32", "uint64"], "thiscall");

rpc.exports = {
  decrypt(src) {
    const instance = Memory.alloc(0x28);
    construct(instance);
    try {
      if (!openFile(instance, Memory.allocUtf16String(src), 1, 0)) throw new Error("QQ 音乐无法打开输入文件");
      const size = getSize(instance);
      if (size === 0) throw new Error("QQ 音乐返回空文件");
      const buffer = Memory.alloc(size);
      const read = readFile(instance, buffer, size, 0);
      if (read !== size) throw new Error("QQ 音乐未完整读取文件");
      return buffer.readByteArray(size);
    } finally {
      destruct(instance);
    }
  }
};
