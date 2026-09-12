# 第三方许可证正文

`web/static/vendor/` 下本地化的前端资源随本项目一起分发，因此按各自许可证的要求
在此附上完整正文。压缩文件头部通常只有一行短声明，完整文本以本目录为准。

| 覆盖的 vendor 文件 | 许可证 | 正文 |
|---|---|---|
| `bootstrap.min.css`、`bootstrap.bundle.min.js`（含打包的 @popperjs/core） | MIT | `bootstrap-MIT.txt` |
| `jquery.min.js` | MIT | `jquery-MIT.txt` |
| `echarts.min.js` | Apache-2.0 | `echarts-Apache-2.0.txt` |
| `echarts-wordcloud.min.js` | ISC | `echarts-wordcloud-ISC.txt` |
| `echarts-wordcloud.min.js` 打包的 wordcloud2.js | MIT | `wordcloud2.js-MIT.txt`（即 `../echarts-wordcloud.min.js.LICENSE.txt` 的副本） |

说明：

- **ECharts 是 Apache-2.0**：其发行包未随附 `NOTICE` 文件，本项目因此也未额外附加
  NOTICE；`echarts.min.js` 文件头保留了 ASF 许可证声明。
- **echarts-wordcloud 声明的是 ISC 而不是 MIT**：上游既没随附 LICENSE 文件也没有头部声明，
  `echarts-wordcloud-ISC.txt` 末尾说明了该正文的来源与署名依据。
- 这些文件是手工本地化的静态快照（见 `../README.md` 的版本表与下载命令）。**升级时不要
  改动文件字节**：报告页依赖 SRI 校验和，任何字节变化都会让浏览器静默拦掉资源。
  许可证正文属于本项目自身的附加文件，不参与 SRI 计算。
