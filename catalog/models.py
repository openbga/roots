from django.db import models


class Method(models.Model):
    """One version of a method. reference and technique are descriptors only: they are not part
    of the identity that locks once work depends on the version."""

    class Reference(models.TextChoices):
        EPA = "epa", "EPA"
        SM = "sm", "Standard Methods"
        SW846 = "sw846", "EPA SW-846"
        ASTM = "astm", "ASTM"
        USGS = "usgs", "USGS"
        LAB = "lab", "Laboratory-developed (SOP)"
        OTHER = "other", "Other"

    class Technique(models.TextChoices):
        ICP_MS = "icp_ms", "ICP-MS"
        ICP_OES = "icp_oes", "ICP-OES"
        CVAA = "cvaa", "Cold vapor AA"
        AA = "aa", "Atomic absorption"
        IC = "ic", "Ion chromatography"
        COLORIMETRIC = "colorimetric", "Colorimetric / spectrophotometric"
        TITRATION = "titration", "Titrimetric"
        GRAVIMETRIC = "gravimetric", "Gravimetric"
        ELECTRODE = "electrode", "Electrode / probe"
        COMBUSTION = "combustion", "Combustion / TOC"
        GC = "gc", "GC"
        GC_MS = "gc_ms", "GC-MS"
        LC_MS = "lc_ms", "LC-MS/MS"
        HPLC = "hplc", "HPLC"
        OTHER = "other", "Other"

    code = models.CharField(max_length=60)
    version = models.CharField(max_length=40)
    title = models.CharField(max_length=200)
    source = models.TextField(blank=True)
    # db_default keeps the previous release's inserts valid while these columns exist.
    reference = models.CharField(
        "Reference source", max_length=20, choices=Reference, blank=True, db_default=""
    )
    technique = models.CharField(
        "Technique", max_length=20, choices=Technique, blank=True, db_default=""
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["code", "version"], name="method_version")]

    def __str__(self):
        return f"{self.code} ({self.version})"


class Analyte(models.Model):
    """A measured substance. group is a descriptor only, outside the locked identity."""

    class Group(models.TextChoices):
        METALS = "metals", "Metals"
        NUTRIENTS = "nutrients", "Nutrients"
        SOLIDS = "solids", "Solids"
        DEMAND = "demand", "Oxygen demand"
        GENERAL = "general", "General chemistry"
        ANIONS = "anions", "Anions"
        PHYSICAL = "physical", "Physical properties"
        VOC = "voc", "Volatile organics"
        SVOC = "svoc", "Semivolatile organics"
        MICRO = "micro", "Microbiology"
        OTHER = "other", "Other"

    name = models.CharField(max_length=160, unique=True)
    cas_number = models.CharField(max_length=30, blank=True)
    # db_default keeps the previous release's inserts valid while this column exists.
    group = models.CharField(
        "Analyte group", max_length=20, choices=Group, blank=True, db_default=""
    )

    def __str__(self):
        return self.name


class TestDefinition(models.Model):
    __test__ = False  # not a pytest class

    method = models.ForeignKey(Method, on_delete=models.PROTECT)
    analyte = models.ForeignKey(Analyte, on_delete=models.PROTECT)
    matrix = models.CharField(
        max_length=20, choices=[("water", "Water"), ("wastewater", "Wastewater"), ("soil", "Soil")]
    )
    unit = models.CharField(max_length=40)
    reporting_limit = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["method", "analyte", "matrix"], name="method_analyte_matrix"
            )
        ]

    def __str__(self):
        return f"{self.analyte} · {self.method} · {self.get_matrix_display()}"
