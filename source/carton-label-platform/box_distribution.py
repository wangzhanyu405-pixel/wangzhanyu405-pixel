def calculate_box_distribution(total_qty, std_qty, pack_info=None):
    """
    根据本次打包总数和装箱参数,计算箱型分布。
    返回 list,每个元素是一个 dict:
        {'box_type': 'full'|'tail'|'pack', 'qty': N, 'pack_count': M(选填)}

    规则(参考你定义的尾数三级处理):
        - 不启用一箱多包:满箱 + 尾箱
        - 启用一箱多包:
            * 满箱:每箱 std_qty (= 每包 × 每箱包数)
            * 尾箱:不满整箱但是整包数(几个完整包)
            * 尾包:剩余的零头散件(不到一包)
    """
    boxes = []
    total = int(total_qty)
    std = int(std_qty)

    if total <= 0 or std <= 0:
        return boxes

    if not pack_info:
        # 简单模式:满箱 + 尾箱
        full_count = total // std
        tail_qty = total % std
        for _ in range(full_count):
            boxes.append({'box_type': 'full', 'qty': std})
        if tail_qty > 0:
            boxes.append({'box_type': 'tail', 'qty': tail_qty})
        return boxes

    # 一箱多包模式
    per_pack = int(pack_info.get('per_pack', 0))
    packs_per_box = int(pack_info.get('packs_per_box', 0))
    if per_pack <= 0 or packs_per_box <= 0:
        return boxes
    box_qty = per_pack * packs_per_box  # = std

    full_packs = total // per_pack          # 整包数
    leftover = total % per_pack             # 零头散件(不到一包)

    full_box_count = full_packs // packs_per_box      # 满箱数
    remaining_packs = full_packs % packs_per_box      # 不够满箱的整包数

    # 满箱
    for _ in range(full_box_count):
        boxes.append({
            'box_type': 'full',
            'qty': box_qty,
            'pack_count': packs_per_box,
        })
    # 尾箱(整包但不满箱)
    if remaining_packs > 0:
        boxes.append({
            'box_type': 'tail',
            'qty': remaining_packs * per_pack,
            'pack_count': remaining_packs,
        })
    # 尾包(不足整包)
    if leftover > 0:
        boxes.append({
            'box_type': 'pack',
            'qty': leftover,
            'pack_count': 1,  # 当一包贴一张
        })
    return boxes
