# -*- coding: utf-8 -*-
"""本地模拟 odom.launch.py 的两个 PythonExpression，验证 use_ekf 组合逻辑。"""
import math

def pe(parts):
    return eval(''.join(parts), {}, math.__dict__)

print("组合 | publish_tf | ekf_on")
print("-----|-----------|-------")
for ekf in ('true', 'false'):
    for src in ('chassis', 'rf2o'):
        ptf_parts = ["'false' if (", "'", ekf, "' == 'true' and '", src, "' == 'chassis') else 'true'"]
        ptf = pe(ptf_parts)
        eon_parts = ["'", ekf, "' == 'true' and '", src, "' == 'chassis'"]
        eon = pe(eon_parts)
        print(f"ekf={ekf:<5} src={src:<7} | {str(ptf):<9} | {eon}")

# 断言：use_ekf=true & chassis → publish_tf=false, ekf_on=True；其余 publish_tf=true
assert pe(["'false' if (", "'true' == 'true' and 'chassis' == 'chassis') else 'true'"]) == 'false'
assert pe(["'true' == 'true' and 'chassis' == 'chassis'"]) is True
assert pe(["'false' if (", "'true' == 'true' and 'rf2o' == 'chassis') else 'true'"]) == 'true'
assert pe(["'false' if (", "'false' == 'true' and 'chassis' == 'chassis') else 'true'"]) == 'true'
print("断言全部通过")
