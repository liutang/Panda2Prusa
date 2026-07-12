from panda2prusa.transform import Transform, compose_chain


def test_parse_and_roundtrip():
    t = Transform.parse("1 0 0 0 1 0 0 0 1 10 20 30")
    assert t.m[9:] == (10.0, 20.0, 30.0)
    assert Transform.parse(t.to_string()).m == t.m


def test_identity_default():
    assert Transform.identity().is_identity()
    assert Transform.parse("").is_identity()
    assert Transform.parse(None).is_identity()


def test_compose_translation():
    # child translates +10x, parent translates +5y -> world +10x +5y
    child = Transform.parse("1 0 0 0 1 0 0 0 1 10 0 0")
    parent = Transform.parse("1 0 0 0 1 0 0 0 1 0 5 0")
    world = child.compose(parent)
    assert world.m[9:] == (10.0, 5.0, 0.0)


def test_compose_scale_then_translate():
    # child scales 2x, parent translates +3x. Point at x=1 -> 2 -> 5.
    child = Transform.parse("2 0 0 0 2 0 0 0 2 0 0 0")
    parent = Transform.parse("1 0 0 0 1 0 0 0 1 3 0 0")
    world = child.compose(parent)
    # scale preserved, translation carried
    assert world.m[0] == 2.0
    assert world.m[9] == 3.0


def test_compose_rotation_order():
    # 90deg about Z then translate; verify translation is not rotated by child
    rot = Transform.parse("0 1 0 -1 0 0 0 0 1 0 0 0")
    trans = Transform.parse("1 0 0 0 1 0 0 0 1 10 0 0")
    world = rot.compose(trans)
    # rotation block preserved, parent translation applied after
    assert world.m[9:] == (10.0, 0.0, 0.0)


def test_compose_chain_identity():
    t = Transform.parse("1 0 0 0 1 0 0 0 1 7 8 9")
    assert compose_chain([t]).m == t.m
    assert compose_chain([Transform.identity(), t]).m == t.m
