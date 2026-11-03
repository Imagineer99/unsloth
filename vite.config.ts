import base from "../../vite.config";
import path from "node:path";
export default {...base,build:{...base.build,rolldownOptions:{...base.build.rolldownOptions,input:path.resolve(__dirname,"index.html")}}};
