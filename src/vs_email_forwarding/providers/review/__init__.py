#!/usr/bin/env python3
"""审查供应商适配。

一期审查是**永远通过的空实现**（``always_pass.py``，恒返回 ``pass``；``NFR-6`` 的显式标注）。
接口协议在 ``base.py``（中性输入对象进、``pass`` / ``reject`` 受控枚举出），实现经
``factory.py`` 按配置注入（``design.md`` §4.2）。

真实审查能力何时补上仍未定（采用本地规则还是外部审查服务亦未选），见 ``docs/open-questions.md``。

不放：业务状态流转。
"""
