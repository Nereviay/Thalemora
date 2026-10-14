# 第三方代码及设计来源声明

洄海的记忆生命周期评分参考并沿用了 Ombre Brain 的短期/长期时间与情绪分段权重设计（3 天阈值、0.7/0.3 权重分配），特别是本项目 `src/thalemora/lifecycle/scoring.py` 的相应分段计算。这套设计来自外部 Ombre Brain 系列实现，特别参考了 Yinglianchun 的二次开发版本；该二改不是本项目开发者所作，不能将该公式标为洄海首创算法。

- 原项目：[Ombre Brain](https://github.com/P0luz/Ombre-Brain)
- 第三方二改来源：[Yinglianchun/Ombre-Brain](https://github.com/Yinglianchun/Ombre-Brain)，维护者 Yinglianchun；该仓库为原版的 fork
- 相关来源：Ombre Brain 原作者 P0lar1zzZ，MIT License
- 本项目其余修改与系统集成采用仓库顶层 Apache-2.0 许可证；原作者 MIT 许可和版权声明完整保留于下方，二者不相互替代。
- 关于源代码复用的核查仍在进行，本声明并不虚构其他第三方代码来源。安装时由包管理器获取的依赖使用各自的独立许可证；其再分发声明须按最终分发内容另行核对。

## Ombre Brain — MIT License（原文）

MIT License

Copyright (c) 2026 P0lar1zzZ

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


## 另行再分发的依赖：补全许可证正文

发布包仅包含洄海自有源码及注明来源的部分；其他 Python 依赖由包管理器按 `requirements.lock` 获取，保留各自分发包中的版权/许可文件。我们已核对实际锁定的 30 个包和安装版本；其中 `idna==3.3`、`jieba==0.42.1` 安装元数据有许可声明，但安装文件列表没有独立 LICENSE/COPYING 文件，因此在这里另外附上对应官方版本的原许可正文。其余依赖若通过 Docker 镜像再分发，依然各守其原许可，不能将它们标为本项目 Apache-2.0 作品。

### idna 3.3 — BSD-3-Clause

官方版本源文件：https://raw.githubusercontent.com/kjd/idna/v3.3/LICENSE.md

```text
BSD 3-Clause License

Copyright (c) 2013-2021, Kim Davies
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### jieba 0.42.1 — MIT

官方版本源文件：https://raw.githubusercontent.com/fxsjy/jieba/v0.42.1/LICENSE

```text
The MIT License (MIT)

Copyright (c) 2013 Sun Junyi

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```
