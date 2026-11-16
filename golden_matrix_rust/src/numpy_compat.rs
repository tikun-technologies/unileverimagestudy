//! NumPy 2.3.2 compatibility primitives.
//!
//! The constants and operation order follow NumPy's `bit_generator.pyx` and
//! `random/src/pcg64/pcg64.h`. Keep this module version-pinned: changing it
//! requires rerunning the Python differential vectors.

const INIT_A: u32 = 0x43b0_d7e5;
const MULT_A: u32 = 0x931e_8875;
const INIT_B: u32 = 0x8b51_f9dd;
const MULT_B: u32 = 0x58f3_8ded;
const MIX_MULT_L: u32 = 0xca01_f9dd;
const MIX_MULT_R: u32 = 0x4973_f715;
const XSHIFT: u32 = 16;

fn hashmix(mut value: u32, hash_const: &mut u32) -> u32 {
    value ^= *hash_const;
    *hash_const = hash_const.wrapping_mul(MULT_A);
    value = value.wrapping_mul(*hash_const);
    value ^ (value >> XSHIFT)
}

fn mix(x: u32, y: u32) -> u32 {
    let result = MIX_MULT_L
        .wrapping_mul(x)
        .wrapping_sub(MIX_MULT_R.wrapping_mul(y));
    result ^ (result >> XSHIFT)
}

fn entropy_words(mut entropy: u128) -> Vec<u32> {
    if entropy == 0 {
        return vec![0];
    }
    let mut words = Vec::new();
    while entropy > 0 {
        words.push(entropy as u32);
        entropy >>= 32;
    }
    words
}

/// NumPy SeedSequence(entropy).generate_state(n, dtype=uint64), for scalar
/// non-negative integer entropy and the default four-word pool.
pub fn seed_sequence_u64(entropy: u128, n_words: usize) -> Vec<u64> {
    let entropy = entropy_words(entropy);
    let mut pool = [0u32; 4];
    let mut hash_const = INIT_A;

    for index in 0..pool.len() {
        pool[index] = hashmix(entropy.get(index).copied().unwrap_or(0), &mut hash_const);
    }
    for source in 0..pool.len() {
        for destination in 0..pool.len() {
            if source != destination {
                let hashed = hashmix(pool[source], &mut hash_const);
                pool[destination] = mix(pool[destination], hashed);
            }
        }
    }
    for value in entropy.iter().skip(pool.len()).copied() {
        for destination in 0..pool.len() {
            pool[destination] = mix(pool[destination], hashmix(value, &mut hash_const));
        }
    }

    let mut generated = Vec::with_capacity(n_words * 2);
    let mut state_hash = INIT_B;
    for index in 0..(n_words * 2) {
        let mut value = pool[index % pool.len()];
        value ^= state_hash;
        state_hash = state_hash.wrapping_mul(MULT_B);
        value = value.wrapping_mul(state_hash);
        value ^= value >> XSHIFT;
        generated.push(value);
    }
    generated
        .chunks_exact(2)
        .map(|words| u64::from(words[0]) | (u64::from(words[1]) << 32))
        .collect()
}

#[derive(Clone, Debug)]
pub struct Pcg64 {
    pub state: u128,
    pub increment: u128,
    pub has_uint32: bool,
    pub cached_uint32: u32,
}

impl Pcg64 {
    const MULTIPLIER: u128 =
        ((2_549_297_995_355_413_924u128) << 64) | 4_865_540_595_714_422_341u128;

    pub fn from_seed_sequence(entropy: u128) -> Self {
        let words = seed_sequence_u64(entropy, 4);
        let initial_state = (u128::from(words[0]) << 64) | u128::from(words[1]);
        let initial_sequence = (u128::from(words[2]) << 64) | u128::from(words[3]);
        let mut generator = Self {
            state: 0,
            increment: initial_sequence.wrapping_shl(1) | 1,
            has_uint32: false,
            cached_uint32: 0,
        };
        generator.step();
        generator.state = generator.state.wrapping_add(initial_state);
        generator.step();
        generator
    }

    pub fn from_raw_state(state: u128, increment: u128, has_uint32: bool, cached_uint32: u32) -> Self {
        Self { state, increment, has_uint32, cached_uint32 }
    }

    fn step(&mut self) {
        self.state = self
            .state
            .wrapping_mul(Self::MULTIPLIER)
            .wrapping_add(self.increment);
    }

    pub fn next_u64(&mut self) -> u64 {
        self.step();
        let high = (self.state >> 64) as u64;
        let low = self.state as u64;
        (high ^ low).rotate_right((high >> 58) as u32)
    }

    pub fn next_u32(&mut self) -> u32 {
        if self.has_uint32 {
            self.has_uint32 = false;
            return self.cached_uint32;
        }
        let value = self.next_u64();
        self.cached_uint32 = (value >> 32) as u32;
        self.has_uint32 = true;
        value as u32
    }

    /// NumPy's 32-bit Lemire sampler for an inclusive range `0..=range`.
    /// This is the path used by `Generator.integers` for the small integer
    /// ranges in the Golden Matrix generator.
    fn bounded_lemire_u32(&mut self, range: u32) -> u32 {
        if range == u32::MAX {
            return self.next_u32();
        }
        let range_exclusive = u64::from(range) + 1;
        let mut product = u64::from(self.next_u32()) * range_exclusive;
        let mut leftover = product as u32;
        if u64::from(leftover) < range_exclusive {
            let threshold = (u64::from(u32::MAX) - u64::from(range)) % range_exclusive;
            while u64::from(leftover) < threshold {
                product = u64::from(self.next_u32()) * range_exclusive;
                leftover = product as u32;
            }
        }
        (product >> 32) as u32
    }

    /// NumPy's bounded uint64 sampler, inclusive at both ends.
    pub fn bounded_u64_inclusive(&mut self, offset: u64, range: u64) -> u64 {
        if range == 0 {
            return offset;
        }
        if range <= u64::from(u32::MAX) {
            return offset + u64::from(self.bounded_lemire_u32(range as u32));
        }
        if range == u64::MAX {
            return offset.wrapping_add(self.next_u64());
        }
        let range_exclusive = u128::from(range) + 1;
        let mut product = u128::from(self.next_u64()) * range_exclusive;
        let mut leftover = product as u64;
        if u128::from(leftover) < range_exclusive {
            let threshold = (u128::from(u64::MAX) - u128::from(range)) % range_exclusive;
            while u128::from(leftover) < threshold {
                product = u128::from(self.next_u64()) * range_exclusive;
                leftover = product as u64;
            }
        }
        offset.wrapping_add((product >> 64) as u64)
    }

    /// NumPy's `random_interval`: mask-and-reject sampling used by shuffle.
    pub fn random_interval(&mut self, max: u64) -> u64 {
        if max == 0 {
            return 0;
        }
        let mut mask = max;
        mask |= mask >> 1;
        mask |= mask >> 2;
        mask |= mask >> 4;
        mask |= mask >> 8;
        mask |= mask >> 16;
        mask |= mask >> 32;
        loop {
            let value = if max <= u64::from(u32::MAX) {
                u64::from(self.next_u32()) & mask
            } else {
                self.next_u64() & mask
            };
            if value <= max {
                return value;
            }
        }
    }

    /// The integer shuffle used internally by NumPy's choice implementation.
    /// Unlike `Generator.shuffle`, this consumes bounded Lemire samples.
    pub fn shuffle_choice_indices(&mut self, values: &mut [usize]) {
        for index in (1..values.len()).rev() {
            let selected = self.bounded_u64_inclusive(0, index as u64) as usize;
            values.swap(index, selected);
        }
    }

    /// NumPy 2.3.2's Floyd/hash-set path for `choice(..., replace=False)`.
    pub fn choice_without_replacement(&mut self, population: usize, size: usize) -> Vec<usize> {
        assert!(size <= population);
        if size == 0 {
            return Vec::new();
        }
        let requested_table_size = (1.2 * size as f64) as usize;
        let mut mask = 1usize;
        while mask < requested_table_size {
            mask = (mask << 1) | 1;
        }
        let sentinel = usize::MAX;
        let mut hash_set = vec![sentinel; mask + 1];
        let mut result = Vec::with_capacity(size);

        for upper in (population - size)..population {
            let value = self.bounded_u64_inclusive(0, upper as u64) as usize;
            let mut location = value & mask;
            while hash_set[location] != sentinel && hash_set[location] != value {
                location = (location + 1) & mask;
            }
            if hash_set[location] == sentinel {
                hash_set[location] = value;
                result.push(value);
            } else {
                let mut upper_location = upper & mask;
                while hash_set[upper_location] != sentinel {
                    upper_location = (upper_location + 1) & mask;
                }
                hash_set[upper_location] = upper;
                result.push(upper);
            }
        }
        self.shuffle_choice_indices(&mut result);
        result
    }

    /// NumPy `Generator.shuffle` operation for a one-dimensional slice.
    pub fn shuffle_slice<T>(&mut self, values: &mut [T]) {
        for index in (1..values.len()).rev() {
            let selected = self.random_interval(index as u64) as usize;
            values.swap(index, selected);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numpy_seed_sequence_vectors_match() {
        assert_eq!(
            seed_sequence_u64(0, 4),
            vec![
                15_793_235_383_387_715_774,
                12_390_638_538_380_655_177,
                2_361_836_109_651_742_017,
                3_188_717_715_514_472_916,
            ]
        );
        assert_eq!(
            seed_sequence_u64(123_000_411, 4),
            vec![
                9_693_880_852_512_313_331,
                763_920_017_128_894_172,
                11_318_523_408_234_731_952,
                5_394_491_297_617_398_032,
            ]
        );
    }

    #[test]
    fn numpy_pcg64_seed_zero_state_and_raw_words_match() {
        let mut generator = Pcg64::from_seed_sequence(0);
        assert_eq!(generator.state, 35_399_562_948_360_463_058_890_781_895_381_311_971);
        assert_eq!(generator.increment, 87_136_372_517_582_989_555_478_159_403_783_844_777);
        let expected = [
            11_749_869_230_777_074_271,
            4_976_686_463_289_251_617,
            755_828_109_848_996_024,
            304_881_062_738_325_533,
            15_002_187_965_291_974_971,
            16_837_368_535_893_154_894,
            11_190_454_901_533_422_207,
            13_456_836_363_123_071_557,
            10_028_111_089_635_196_863,
            17_249_041_691_996_241_901,
        ];
        assert_eq!(generator.next_u64(), expected[0]);
        for value in expected.iter().skip(1) {
            assert_eq!(generator.next_u64(), *value);
        }
    }

    #[test]
    fn numpy_pcg64_attempt_seed_state_and_raw_words_match() {
        let mut generator = Pcg64::from_seed_sequence(123_000_411);
        assert_eq!(generator.state, 319_554_754_629_480_174_175_594_406_588_263_216_210);
        assert_eq!(generator.increment, 77_297_442_287_055_291_754_107_379_683_203_453_473);
        let expected = [
            146_657_966_145_011_234,
            467_745_018_031_753_286,
            3_818_789_325_849_743_211,
            778_306_365_087_367_066,
            16_388_580_436_483_844_957,
            652_956_689_542_104_037,
            13_522_698_621_959_061_780,
            1_608_891_017_157_783_689,
            4_345_506_019_633_055_993,
            992_372_452_673_191_135,
        ];
        for value in expected {
            assert_eq!(generator.next_u64(), value);
        }
    }

    #[test]
    fn numpy_bounded_integer_stream_matches() {
        let mut generator = Pcg64::from_seed_sequence(0);
        let actual: Vec<u64> = (0..8)
            .map(|_| generator.bounded_u64_inclusive(2, 3))
            .collect();
        assert_eq!(actual, vec![5, 4, 4, 3, 3, 2, 2, 2]);
    }

    #[test]
    fn numpy_choice_stream_matches() {
        let mut generator = Pcg64::from_seed_sequence(0);
        assert_eq!(generator.choice_without_replacement(3, 2), vec![1, 2]);
        assert_eq!(generator.choice_without_replacement(3, 3), vec![1, 2, 0]);
        assert_eq!(generator.choice_without_replacement(5, 1), vec![0]);
        assert_eq!(generator.choice_without_replacement(5, 3), vec![0, 3, 4]);
        assert_eq!(
            generator.choice_without_replacement(8, 5),
            vec![7, 4, 2, 6, 5]
        );
    }

    #[test]
    fn numpy_shuffle_stream_matches() {
        let mut generator = Pcg64::from_seed_sequence(0);
        let mut actual: Vec<usize> = (0..10).collect();
        generator.shuffle_slice(&mut actual);
        assert_eq!(actual, vec![4, 6, 2, 7, 3, 5, 9, 0, 8, 1]);
    }

    #[test]
    fn zero_width_operations_do_not_consume_rng() {
        let mut generator = Pcg64::from_seed_sequence(0);
        let initial = (generator.state, generator.has_uint32, generator.cached_uint32);
        assert_eq!(generator.bounded_u64_inclusive(7, 0), 7);
        assert_eq!(generator.choice_without_replacement(1, 1), vec![0]);
        let mut singleton = vec![42usize];
        generator.shuffle_slice(&mut singleton);
        assert_eq!(singleton, vec![42]);
        assert_eq!(initial, (generator.state, generator.has_uint32, generator.cached_uint32));
    }
}
